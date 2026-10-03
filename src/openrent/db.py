"""Normalized, idempotent SQLite storage for listings and downloaded images."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from . import filtering
from .models import Feature, Image, Property

SCHEMA_VERSION = 6
REVIEW_SCHEMA_VERSION = 4

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
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def review_path_for(path: str | Path) -> Path | None:
    """Append to the resolved filename so different archive extensions cannot collide."""
    return None if str(path) == ":memory:" else Path(str(Path(path).resolve()) + ".review.sqlite")


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
        self.review_path = review_path_for(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            try:
                self.connection.execute("SELECT jsonb('{}')").fetchone()
            except sqlite3.OperationalError as exc:
                raise ValueError(
                    "Review storage requires SQLite 3.45 or newer with native JSONB support."
                ) from exc
            self.connection.execute(
                "ATTACH DATABASE ? AS review",
                (str(self.review_path) if self.review_path is not None else ":memory:",),
            )
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"Database schema version {version} is newer than supported version "
                    f"{SCHEMA_VERSION}; upgrade openrent-fetch before opening it."
                )
            review_version = self.connection.execute("PRAGMA review.user_version").fetchone()[0]
            if review_version > REVIEW_SCHEMA_VERSION:
                raise ValueError("Review database was upgraded by a newer application.")
            if version < SCHEMA_VERSION or review_version < REVIEW_SCHEMA_VERSION:
                # Moving legacy rows between files requires rollback journals
                # for crash atomicity. A sidecar-only upgrade writes one file,
                # so it remains atomic in WAL and can coexist with open scanners.
                if version < SCHEMA_VERSION:
                    for namespace in ("main", "review"):
                        mode = self.connection.execute(
                            f"PRAGMA {namespace}.journal_mode = DELETE"
                        ).fetchone()[0]
                        if self.path is not None and mode != "delete":
                            raise ValueError(
                                "Cannot obtain rollback journals for the schema migration."
                            )
                with self.connection:
                    self.connection.execute("BEGIN IMMEDIATE")
                    version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                    review_version = self.connection.execute(
                        "PRAGMA review.user_version"
                    ).fetchone()[0]
                    if version > SCHEMA_VERSION or review_version > REVIEW_SCHEMA_VERSION:
                        raise ValueError("Database was upgraded by a newer application.")
                    if "search_id" in {
                        row["name"]
                        for row in self.connection.execute(
                            "PRAGMA review.table_info(property_reviews)"
                        )
                    }:
                        self.connection.execute(
                            "ALTER TABLE review.property_reviews DROP COLUMN search_id"
                        )
                    self._migrate_review_notifications(review_version)
                    self._execute_schema("review_schema.sql")
                    if version < SCHEMA_VERSION:
                        self._migrate_legacy_reviews(version)
                        for table in ("search_matches", "search_filters", "searches"):
                            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
                        self._execute_schema("schema.sql")
            self.connection.execute("PRAGMA main.journal_mode = WAL")
            self.connection.execute("PRAGMA review.journal_mode = WAL")
        except Exception:
            self.connection.close()
            raise

    def _execute_schema(self, filename: str) -> None:
        """Execute statements without executescript's implicit transaction commit."""
        schema = Path(__file__).with_name(filename).read_text(encoding="utf-8")
        statement = ""
        for line in schema.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                self.connection.execute(statement)
                statement = ""

    def _main_table_exists(self, table: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            is not None
        )

    def _migrate_review_notifications(self, version: int) -> None:
        """Rebuild the v3 outbox atomically, retaining history and safe retry bodies."""
        if (
            version >= 4
            or self.connection.execute(
                "SELECT 1 FROM review.sqlite_master WHERE type = 'table' AND name = 'email_batches'"
            ).fetchone()
            is None
        ):
            return
        from .email_sender import SENDER_EMAIL

        # Renaming both tables retains the old composite foreign key until
        # their rows have been copied to the freshly constrained pair.
        self.connection.execute(
            "ALTER TABLE review.email_batch_items RENAME TO email_batch_items_v3"
        )
        self.connection.execute("ALTER TABLE review.email_batches RENAME TO email_batches_v3")
        self.connection.execute("DROP INDEX IF EXISTS review.email_batches_recipient_status")
        self._execute_schema("review_schema.sql")
        self.connection.execute(
            "INSERT INTO review.email_batches "
            "(id, recipient, sender, subject, html_body, text_body, status, provider_message_id, "
            "error, created_at, last_attempt_at, sent_at, attempts) "
            "SELECT id, recipient, "
            "CASE WHEN sender = 'flats@spanashis.com' AND status IN ('pending', 'failed') "
            "THEN ? ELSE sender END, subject, html_body, text_body, status, provider_message_id, "
            "error, created_at, last_attempt_at, sent_at, attempts FROM review.email_batches_v3",
            (SENDER_EMAIL,),
        )
        self.connection.execute(
            "INSERT INTO review.email_batch_items (batch_id, recipient, property_id, review_id) "
            "SELECT batch_id, recipient, property_id, review_id FROM review.email_batch_items_v3"
        )
        self.connection.execute("DROP TABLE review.email_batch_items_v3")
        self.connection.execute("DROP TABLE review.email_batches_v3")
        if self.connection.execute("PRAGMA review.foreign_key_check").fetchall():
            raise ValueError("Notification migration left invalid foreign keys")

    def _migrate_legacy_reviews(self, version: int) -> None:
        """Copy stable legacy IDs, verify conflicts, then remove old main tables."""
        if self._main_table_exists("review_profiles"):
            for row in self.connection.execute("SELECT * FROM main.review_profiles").fetchall():
                backend = row["backend"] if "backend" in dict(row) else "codex"
                existing = self.connection.execute(
                    "SELECT * FROM review.review_profiles WHERE profile_key = ?",
                    (row["profile_key"],),
                ).fetchone()
                if existing is not None:
                    if (existing["criteria"], existing["model"], existing["backend"]) != (
                        row["criteria"],
                        row["model"],
                        backend,
                    ):
                        raise ValueError(
                            "Legacy review profile conflicts with the review database."
                        )
                else:
                    self.connection.execute(
                        "INSERT INTO review.review_profiles "
                        "(profile_key, criteria, model, backend, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            row["profile_key"],
                            row["criteria"],
                            row["model"],
                            backend,
                            row["created_at"],
                        ),
                    )
        if self._main_table_exists("property_reviews"):
            has_findings = self._main_table_exists("review_findings")
            has_images = self._main_table_exists("review_images")
            for row in self.connection.execute("SELECT * FROM main.property_reviews").fetchall():
                findings = (
                    [
                        dict(item)
                        for item in self.connection.execute(
                            "SELECT position, criterion, outcome, evidence FROM main.review_findings "
                            "WHERE review_id = ? ORDER BY position",
                            (row["id"],),
                        )
                    ]
                    if has_findings
                    else []
                )
                images = (
                    self.connection.execute(
                        "SELECT * FROM main.review_images WHERE review_id = ? ORDER BY position",
                        (row["id"],),
                    ).fetchall()
                    if has_images
                    else []
                )
                result = None
                if row["status"] == "complete" or findings or row["decision"] or row["summary"]:
                    result = json.dumps(
                        {
                            "decision": row["decision"],
                            "summary": row["summary"],
                            "findings": findings,
                            "images_examined": [image["position"] + 1 for image in images],
                            "legacy_schema_version": version,
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                existing = self.connection.execute(
                    "SELECT *, json(result) AS result_text FROM review.property_reviews WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                scalar = dict(row)
                scalar.pop("result", None)
                scalar.pop("search_id", None)
                scalar.pop("decision")
                scalar.pop("summary")
                if existing is not None:
                    if any(existing[name] != value for name, value in scalar.items()) or (
                        (json.loads(existing["result_text"]) if existing["result_text"] else None)
                        != (json.loads(result) if result else None)
                    ):
                        raise ValueError("Legacy review ID conflicts with the review database.")
                else:
                    columns = ", ".join(scalar)
                    values = ", ".join("?" for _ in scalar)
                    self.connection.execute(
                        f"INSERT INTO review.property_reviews ({columns}, result) "
                        f"VALUES ({values}, jsonb(?))",
                        (*scalar.values(), result),
                    )
                for image in images:
                    image_values = dict(image)
                    existing_image = self.connection.execute(
                        "SELECT * FROM review.review_images WHERE review_id = ? AND position = ?",
                        (image["review_id"], image["position"]),
                    ).fetchone()
                    if existing_image is not None:
                        if dict(existing_image) != image_values:
                            raise ValueError(
                                "Legacy review gallery conflicts with the review database."
                            )
                    else:
                        columns = ", ".join(image_values)
                        values = ", ".join("?" for _ in image_values)
                        self.connection.execute(
                            f"INSERT INTO review.review_images ({columns}) VALUES ({values})",
                            tuple(image_values.values()),
                        )
        for table in (
            "review_digest_items",
            "review_digests",
            "review_findings",
            "review_images",
            "property_reviews",
            "review_profiles",
        ):
            self.connection.execute(f"DROP TABLE IF EXISTS main.{table}")

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

    def _delete_properties(self, property_ids: list[int]) -> int:
        """Delete inside the caller's transaction, retaining all shared image bytes."""
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ValueError("Property deletion requires enabled foreign keys.")
        contents = set()
        deleted = 0
        for batch in batched(property_ids, 500):
            placeholders = ",".join("?" for _ in batch)
            # Reviews can reference old gallery bytes no longer in property_images.
            contents.update(
                row[0]
                for row in self.connection.execute(
                    "SELECT content_sha256 FROM property_images "
                    f"WHERE property_id IN ({placeholders}) AND content_sha256 IS NOT NULL "
                    "UNION SELECT i.sha256 FROM review.review_images i "
                    "JOIN review.property_reviews r ON r.id = i.review_id "
                    f"WHERE r.property_id IN ({placeholders})",
                    (*batch, *batch),
                )
            )
            self.connection.execute(
                f"DELETE FROM review.property_reviews WHERE property_id IN ({placeholders})", batch
            )
            deleted += self.connection.execute(
                f"DELETE FROM properties WHERE id IN ({placeholders})", batch
            ).rowcount
        for batch in batched(contents, 500):
            placeholders = ",".join("?" for _ in batch)
            self.connection.execute(
                f"DELETE FROM image_blobs WHERE sha256 IN ({placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM property_images "
                "WHERE content_sha256 = image_blobs.sha256) "
                "AND NOT EXISTS (SELECT 1 FROM review.review_images "
                "WHERE sha256 = image_blobs.sha256)",
                batch,
            )
        return deleted

    def delete_properties(self, property_ids: list[int]) -> int:
        """Atomically cascade property deletion and clean up their unreferenced BLOBs."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            return self._delete_properties(list(dict.fromkeys(property_ids)))

    def filter_properties(self, property_ids: list[int]) -> list[int]:
        """Evaluate saved facts and atomically delete rejected IDs from this scan only."""
        property_ids = list(dict.fromkeys(property_ids))
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            passing = list(
                dict.fromkeys(filtering.filter_property_ids(self.connection, property_ids))
            )
            if not set(passing) <= set(property_ids):
                raise ValueError("Post-filter returned IDs outside the current scan.")
            keep = set(passing)
            self._delete_properties(
                [property_id for property_id in property_ids if property_id not in keep]
            )
            return passing

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
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            now = _now()
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
        *,
        require_association: bool = False,
    ) -> bool:
        """Store actual image bytes, deduplicating content across listings and URLs.

        Returns False if this association already points to the same image bytes.
        Download headers belong to the source URL, rather than the shared blob.
        With require_association, a URL removed during download stays removed.
        """
        if not content:
            raise ValueError("Cannot store an empty image")
        sha256 = hashlib.sha256(content).hexdigest()
        now = _now()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.get_property(property_id) is None:
                return False  # A concurrent deletion filter may have removed this listing.
            if (
                require_association
                and self.connection.execute(
                    "SELECT 1 FROM property_images WHERE property_id = ? AND source_url = ?",
                    (property_id, image.source_url),
                ).fetchone()
                is None
            ):
                return False
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
            self.connection.execute("BEGIN IMMEDIATE")
            if self.get_property(property_id) is None:
                return
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
