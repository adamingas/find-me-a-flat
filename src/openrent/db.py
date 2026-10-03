"""Normalized, idempotent SQLite storage for listings and downloaded images."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .models import Candidate, Feature, Image, Property

SCHEMA_VERSION = 1

# Keep this explicit: a dataclass addition must be accompanied by a schema change.
PROPERTY_COLUMNS = (
    "url",
    "title",
    "description",
    "description_html",
    "property_type",
    "property_type_code",
    "address_display",
    "locality",
    "postcode",
    "country",
    "latitude",
    "longitude",
    "bedrooms",
    "bathrooms",
    "max_tenants",
    "rent_pcm_pence",
    "rent_weekly_pence",
    "deposit_pence",
    "currency",
    "available_from",
    "minimum_tenancy_months",
    "maximum_tenancy_months",
    "furnished",
    "unfurnished",
    "furnishing",
    "bills_included",
    "pets_allowed",
    "students_allowed",
    "non_students_allowed",
    "families_allowed",
    "dss_covers_rent",
    "garden",
    "parking",
    "fireplace",
    "smokers_allowed",
    "has_video",
    "video_viewings",
    "is_shared",
    "is_studio",
    "is_live",
    "status",
    "first_listed_at",
    "epc_rating",
    "landlord_name",
    "landlord_member_since",
    "landlord_last_active",
    "source_html",
)

TABLES = (
    "properties",
    "property_images",
    "image_blobs",
    "property_features",
    "nearby_places",
    "media_links",
    "searches",
    "search_filters",
    "search_matches",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _merge_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """A partial response must not erase previously obtained nested information."""
    merged = existing.copy()
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_metadata(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


class Database:
    """Open an archive, initialize its schema, and commit each operation atomically.

    Partial search records preserve known values and child associations. A
    ``Property(detail_complete=True)`` synchronizes its current child lists,
    including removing associations that disappeared from the detail page.
    Removed image associations do not delete their content-addressed image bytes.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"Database schema version {version} is newer than supported version "
                    f"{SCHEMA_VERSION}; upgrade openrent-fetch before opening it."
                )
            if version < SCHEMA_VERSION:
                schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                self.connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            self.connection.execute("PRAGMA journal_mode = WAL")
        except Exception:
            self.connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def get_property(self, property_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM properties WHERE id = ?", (property_id,)
        ).fetchone()

    def _upsert_row(
        self,
        table: str,
        keys: dict[str, Any],
        values: dict[str, Any],
        now: str,
        *,
        preserve_unknown: bool = False,
    ) -> bool:
        """Private helper: table/column names come only from constants in this module."""
        where = " AND ".join(f"{key} = ?" for key in keys)
        existing = self.connection.execute(
            f"SELECT * FROM {table} WHERE {where}", tuple(keys.values())
        ).fetchone()
        if existing is None:
            record = {
                **keys,
                **values,
                "first_seen_at": now,
                "last_seen_at": now,
                "updated_at": now,
            }
            columns = ", ".join(record)
            placeholders = ", ".join("?" for _ in record)
            self.connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(record.values())
            )
            return True
        if preserve_unknown:
            values = {
                key: existing[key] if value is None else value for key, value in values.items()
            }
        changes = {key: value for key, value in values.items() if existing[key] != value}
        if changes:
            changes["updated_at"] = now
        changes["last_seen_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in changes)
        self.connection.execute(
            f"UPDATE {table} SET {assignments} WHERE {where}",
            (*changes.values(), *keys.values()),
        )
        return False

    def upsert_property(self, property: Property) -> bool:
        """Insert or refresh one listing; return True only for a new property ID."""
        now = _now()
        with self.connection:
            existing = self.get_property(property.id)
            values = {column: getattr(property, column) for column in PROPERTY_COLUMNS}
            # SQLite stores booleans as integers, preserving None for unknown facts.
            values = {
                key: int(value) if isinstance(value, bool) else value
                for key, value in values.items()
            }
            metadata = property.extra_metadata
            if existing is not None and not property.detail_complete:
                metadata = _merge_metadata(json.loads(existing["extra_metadata_json"]), metadata)
            values["extra_metadata_json"] = json.dumps(
                metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            inserted = self._upsert_row(
                "properties", {"id": property.id}, values, now, preserve_unknown=True
            )
            self._sync_children(property, now)
        return inserted

    @staticmethod
    def _feature_values(feature: Feature) -> dict[str, Any]:
        values: dict[str, Any] = {
            "label": feature.label,
            "value_type": None,
            "value_text": None,
            "value_integer": None,
            "value_real": None,
            "value_boolean": None,
            "unit": feature.unit,
        }
        value = feature.value
        if isinstance(value, bool):
            values.update(value_type="boolean", value_boolean=int(value))
        elif isinstance(value, int):
            values.update(value_type="integer", value_integer=value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"Feature {feature.key!r} contains a non-finite number")
            values.update(value_type="real", value_real=value)
        elif isinstance(value, str):
            values.update(value_type="text", value_text=value)
        elif value is not None:
            raise TypeError(f"Feature {feature.key!r} must have a scalar value")
        return values

    def _sync_children(self, property: Property, now: str) -> None:
        features = []
        for feature in property.features:
            keys = {
                "property_id": property.id,
                "section": feature.section,
                "feature_key": feature.key,
            }
            features.append(keys)
            values = self._feature_values(feature)
            if not property.detail_complete:
                existing = self.connection.execute(
                    "SELECT * FROM property_features "
                    "WHERE property_id = ? AND section = ? AND feature_key = ?",
                    tuple(keys.values()),
                ).fetchone()
                if existing:
                    if feature.value is None:
                        for column in (
                            "value_type",
                            "value_text",
                            "value_integer",
                            "value_real",
                            "value_boolean",
                        ):
                            values[column] = existing[column]
                    if feature.unit is None:
                        values["unit"] = existing["unit"]
            # The typed value columns are a single unit: retaining a previous
            # type's value alongside a new type would violate the scalar check.
            self._upsert_row("property_features", keys, values, now)

        images = []
        for image in property.images:
            images.append({"property_id": property.id, "source_url": image.source_url})
            self._upsert_image(property.id, image, now)

        places = []
        for place in property.nearby_places:
            keys = {"property_id": property.id, "name": place.name, "kind": place.kind}
            places.append(keys)
            self._upsert_row(
                "nearby_places",
                keys,
                {
                    "walking_minutes": place.walking_minutes,
                    "distance_km": place.distance_km,
                    "latitude": place.latitude,
                    "longitude": place.longitude,
                },
                now,
                preserve_unknown=not property.detail_complete,
            )

        links = []
        for link in property.media_links:
            keys = {"property_id": property.id, "url": link.url, "kind": link.kind}
            links.append(keys)
            self._upsert_row(
                "media_links",
                keys,
                {"caption": link.caption},
                now,
                preserve_unknown=not property.detail_complete,
            )

        if property.detail_complete:
            for table, keys in (
                ("property_features", features),
                ("property_images", images),
                ("nearby_places", places),
                ("media_links", links),
            ):
                self._remove_stale_children(table, property.id, keys)

    def _remove_stale_children(
        self, table: str, property_id: int, current: list[dict[str, Any]]
    ) -> None:
        # Compare composite keys in Python to avoid SQLite's bound-parameter limit
        # even on unusually large galleries or amenity lists.
        if not current:
            self.connection.execute(f"DELETE FROM {table} WHERE property_id = ?", (property_id,))
            return
        columns = tuple(current[0])
        keep = {tuple(keys[column] for column in columns) for keys in current}
        rows = self.connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE property_id = ?", (property_id,)
        ).fetchall()
        stale = [tuple(row) for row in rows if tuple(row) not in keep]
        where = " AND ".join(f"{column} = ?" for column in columns)
        self.connection.executemany(f"DELETE FROM {table} WHERE {where}", stale)

    def _upsert_image(self, property_id: int, image: Image, now: str) -> bool:
        return self._upsert_row(
            "property_images",
            {"property_id": property_id, "source_url": image.source_url},
            {
                "position": image.position,
                "kind": image.kind,
                "caption": image.caption,
                "width": image.width,
                "height": image.height,
            },
            now,
            preserve_unknown=True,
        )

    def image_downloaded(self, property_id: int, url: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM property_images WHERE property_id = ? AND source_url = ? "
                "AND download_status = 'downloaded' AND content_sha256 IS NOT NULL",
                (property_id, url),
            ).fetchone()
            is not None
        )

    def store_image(
        self,
        property_id: int,
        image: Image,
        content: bytes,
        content_type: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        """Store actual image bytes, deduplicating content across listings and URLs.

        Returns False if this association already points to the same image bytes.
        Download headers belong to the source URL, rather than the shared blob.
        """
        if not content:
            raise ValueError("Cannot store an empty image")
        sha256 = hashlib.sha256(content).hexdigest()
        now = _now()
        with self.connection:
            self._upsert_image(property_id, image, now)
            existing = self.connection.execute(
                "SELECT * FROM property_images WHERE property_id = ? AND source_url = ?",
                (property_id, image.source_url),
            ).fetchone()
            changed = existing["content_sha256"] != sha256
            self.connection.execute(
                "INSERT OR IGNORE INTO image_blobs "
                "(sha256, content, byte_length, content_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (sha256, content, len(content), content_type, now),
            )
            self._upsert_row(
                "property_images",
                {
                    "property_id": property_id,
                    "source_url": image.source_url,
                },
                {
                    "content_sha256": sha256,
                    "download_status": "downloaded",
                    "etag": etag if etag is not None else existing["etag"],
                    "last_modified": (
                        last_modified if last_modified is not None else existing["last_modified"]
                    ),
                    "last_error": None,
                    "downloaded_at": now if changed else existing["downloaded_at"],
                },
                now,
            )
        return changed

    def record_image_error(self, property_id: int, url: str, message: str) -> None:
        now = _now()
        with self.connection:
            # Preserve an already downloaded image if a subsequent request fails.
            self.connection.execute(
                "INSERT OR IGNORE INTO property_images "
                "(property_id, source_url, first_seen_at, last_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (property_id, url, now, now, now),
            )
            existing = self.connection.execute(
                "SELECT download_status FROM property_images WHERE property_id = ? AND source_url = ?",
                (property_id, url),
            ).fetchone()
            self._upsert_row(
                "property_images",
                {"property_id": property_id, "source_url": url},
                {
                    "download_status": (
                        "downloaded" if existing["download_status"] == "downloaded" else "error"
                    ),
                    "last_error": message,
                },
                now,
            )

    @staticmethod
    def search_id_for(params: dict[str, str], location: str) -> str:
        """Resolve an existing search's stable ID without creating or changing it."""
        canonical_params = {str(key): str(value) for key, value in sorted(params.items())}
        canonical = json.dumps(
            [" ".join(location.split()).casefold(), canonical_params],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def upsert_search(
        self,
        params: dict[str, str],
        location: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> str:
        """Return the stable search ID for a location and set of scalar filters."""
        canonical_params = {str(key): str(value) for key, value in sorted(params.items())}
        normalized_location = " ".join(location.split())
        search_id = self.search_id_for(params, location)
        now = _now()
        with self.connection:
            self._upsert_row(
                "searches",
                {"id": search_id},
                {
                    "location": normalized_location,
                    "latitude": latitude,
                    "longitude": longitude,
                    # A process can be interrupted before finish_search runs.
                    # Keep the last successful completion timestamp, but never
                    # describe a newly started execution as already complete.
                    "last_search_complete": 0,
                },
                now,
                preserve_unknown=True,
            )
            self.connection.executemany(
                "INSERT INTO search_filters (search_id, name, value) VALUES (?, ?, ?) "
                "ON CONFLICT(search_id, name) DO UPDATE SET value = excluded.value",
                [(search_id, name, value) for name, value in canonical_params.items()],
            )
        return search_id

    def record_match(self, search_id: str, candidate: Candidate) -> None:
        now = _now()
        # The importer normally already saved the property, including details.
        # Standalone callers can pass candidates directly without another step.
        if self.get_property(candidate.property.id) is None:
            self.upsert_property(candidate.property)
        with self.connection:
            self._upsert_row(
                "search_matches",
                {
                    "search_id": search_id,
                    "property_id": candidate.property.id,
                },
                {
                    "distance_km": candidate.distance_km,
                    "commute_minutes": candidate.commute_minutes,
                    "active": 1,
                },
                now,
                preserve_unknown=True,
            )

    def finish_search(self, search_id: str, seen_ids: list[int], complete: bool) -> None:
        """Deactivate missing matches only after a full, successfully imported search."""
        now = _now()
        with self.connection:
            if complete:
                keep = set(seen_ids)
                matches = self.connection.execute(
                    "SELECT property_id FROM search_matches WHERE search_id = ? AND active = 1",
                    (search_id,),
                ).fetchall()
                self.connection.executemany(
                    "UPDATE search_matches SET active = 0, updated_at = ? "
                    "WHERE search_id = ? AND property_id = ?",
                    [(now, search_id, row[0]) for row in matches if row[0] not in keep],
                )
            values: dict[str, Any] = {"last_search_complete": int(complete)}
            if complete:
                values["last_completed_at"] = now
            self._upsert_row("searches", {"id": search_id}, values, now)

    def counts(self) -> dict[str, int]:
        result = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
        for status, name in (
            ("downloaded", "downloaded_images"),
            ("pending", "pending_images"),
            ("error", "failed_images"),
        ):
            result[name] = self.connection.execute(
                "SELECT count(*) FROM property_images WHERE download_status = ?", (status,)
            ).fetchone()[0]
        return result
