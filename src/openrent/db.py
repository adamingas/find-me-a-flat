"""Normalized, idempotent SQLite storage for listings and downloaded images."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import aiosqlite

from .email_sender import SENDER_EMAIL
from .evidence import ReviewSnapshot, load_evidence
from .models import Feature, Image, Property

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
    "openrent_commute_minutes",
    "search_target",
    "search_target_latitude",
    "search_target_longitude",
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


_locks: WeakKeyDictionary[aiosqlite.Connection, asyncio.Lock] = WeakKeyDictionary()


async def _migrate_openrent_commute(connection: aiosqlite.Connection) -> None:
    """Store OpenRent search facts on properties, including earlier misplaced payloads."""
    definitions = {
        "openrent_commute_minutes": "REAL CHECK (openrent_commute_minutes >= 0)",
        "search_target": "TEXT",
        "search_target_latitude": "REAL CHECK (search_target_latitude BETWEEN -90 AND 90)",
        "search_target_longitude": "REAL CHECK (search_target_longitude BETWEEN -180 AND 180)",
    }
    async with _operation(connection, write=True):
        columns = {
            row["name"]
            for row in await connection.execute_fetchall("PRAGMA table_info(properties)")
        }
        for name, definition in definitions.items():
            if name not in columns:
                await connection.execute(f"ALTER TABLE properties ADD COLUMN {name} {definition}")
        await connection.execute(
            "UPDATE properties SET "
            "openrent_commute_minutes = json_extract(a.data, '$.duration_minutes'), "
            "search_target = json_extract(a.data, '$.target'), "
            "search_target_latitude = json_extract(a.data, '$.target_latitude'), "
            "search_target_longitude = json_extract(a.data, '$.target_longitude') "
            "FROM amenities a WHERE a.property_id = properties.id AND a.type = 'openrent_commute' "
            "AND properties.openrent_commute_minutes IS NULL"
        )
        await connection.execute("DELETE FROM amenities WHERE type = 'openrent_commute'")


@asynccontextmanager
async def connect(path: str | Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(
        str(path), timeout=30, uri=True, isolation_level=None
    ) as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        await connection.execute("PRAGMA journal_mode = WAL")
        _locks[connection] = asyncio.Lock()
        try:
            await _migrate_openrent_commute(connection)
            yield connection
        finally:
            del _locks[connection]


@asynccontextmanager
async def _operation(connection: aiosqlite.Connection, *, write: bool = False):
    # Serialize complete operations: aiosqlite queues individual statements only.
    async with _locks[connection]:
        try:
            if write:
                await connection.execute("BEGIN IMMEDIATE")
            yield
            if write:
                await connection.commit()
        except BaseException:
            if write:
                await connection.rollback()
            raise


async def _fetch_one(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


async def _fetch_all(connection, sql, parameters=()):
    return await connection.execute_fetchall(sql, parameters)


async def fetch_one(
    connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
) -> aiosqlite.Row | None:
    async with _operation(connection):
        return await _fetch_one(connection, sql, parameters)


async def fetch_all(
    connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
) -> list[aiosqlite.Row]:
    async with _operation(connection):
        return await _fetch_all(connection, sql, parameters)


async def get_property(connection: aiosqlite.Connection, property_id: int) -> aiosqlite.Row | None:
    async with _operation(connection):
        return await _fetch_one(connection, "SELECT * FROM properties WHERE id = ?", (property_id,))


async def query(
    connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
) -> list[dict[str, Any]]:
    """Execute parameterized SQL for property filters and information generators."""
    async with _operation(connection):
        return [dict(row) for row in await _fetch_all(connection, sql, parameters)]


async def get_amenities(connection: aiosqlite.Connection, property_id: int) -> dict[str, Any]:
    async with _operation(connection):
        return {
            row["type"]: json.loads(row["data"])
            for row in await _fetch_all(
                connection,
                "SELECT type, json(data) AS data FROM amenities WHERE property_id = ? ORDER BY type",
                (property_id,),
            )
        }


async def store_amenity(
    connection: aiosqlite.Connection, property_id: int, amenity_type: str, data: str
) -> bool:
    """Save the first payload for this type; subsequent writes preserve it."""
    async with _operation(connection, write=True):
        cursor = await connection.execute(
            (
                "INSERT INTO amenities (property_id, type, data) VALUES (?, ?, jsonb(?)) ON "
                "CONFLICT(property_id, type) DO NOTHING"
            ),
            (property_id, amenity_type, data),
        )
        return cursor.rowcount == 1


async def get_images(connection: aiosqlite.Connection, property_id: int) -> list[Image]:
    async with _operation(connection):
        return [
            Image(**{name: row[name] for name in Image.__dataclass_fields__})
            for row in await _fetch_all(
                connection, "SELECT * FROM property_images WHERE property_id = ?", (property_id,)
            )
        ]


async def _upsert_row(
    connection: aiosqlite.Connection,
    table: str,
    keys: dict[str, Any],
    values: dict[str, Any],
    now: str,
    *,
    preserve_unknown: bool = False,
) -> bool:
    """Private helper: table/column names come only from constants in this module."""
    where = " AND ".join(f"{key} = ?" for key in keys)
    existing = await _fetch_one(
        connection, f"SELECT * FROM {table} WHERE {where}", tuple(keys.values())
    )
    if existing is None:
        record = {**keys, **values, "first_seen_at": now, "last_seen_at": now, "updated_at": now}
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        await connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(record.values())
        )
        return True
    if preserve_unknown:
        values = {key: existing[key] if value is None else value for key, value in values.items()}
    changes = {key: value for key, value in values.items() if existing[key] != value}
    if changes:
        changes["updated_at"] = now
    changes["last_seen_at"] = now
    assignments = ", ".join(f"{key} = ?" for key in changes)
    await connection.execute(
        f"UPDATE {table} SET {assignments} WHERE {where}", (*changes.values(), *keys.values())
    )
    return False


async def upsert_property(connection: aiosqlite.Connection, property: Property) -> bool:
    """Insert or refresh one listing; return True only for a new property ID."""
    async with _operation(connection, write=True):
        now = _now()
        existing = await _fetch_one(
            connection, "SELECT * FROM properties WHERE id = ?", (property.id,)
        )
        values = {column: getattr(property, column) for column in PROPERTY_COLUMNS}
        # Preserve unknown facts; SQLite stores known booleans as integers.
        values = {
            key: int(value) if isinstance(value, bool) else value for key, value in values.items()
        }
        metadata = property.extra_metadata
        if existing is not None and not property.detail_complete:
            metadata = _merge_metadata(json.loads(existing["extra_metadata_json"]), metadata)
        values["extra_metadata_json"] = json.dumps(
            metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        inserted = await _upsert_row(
            connection, "properties", {"id": property.id}, values, now, preserve_unknown=True
        )
        await _sync_children(connection, property, now)
    return inserted


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


async def _sync_children(connection: aiosqlite.Connection, property: Property, now: str) -> None:
    features = []
    for feature in property.features:
        keys = {"property_id": property.id, "section": feature.section, "feature_key": feature.key}
        features.append(keys)
        values = _feature_values(feature)
        if not property.detail_complete:
            existing = await _fetch_one(
                connection,
                "SELECT * FROM property_features WHERE property_id = ? AND section = ? AND feature_key = ?",
                tuple(keys.values()),
            )
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
        # Replace the scalar type and value together to satisfy the typed-value check.
        await _upsert_row(connection, "property_features", keys, values, now)
    images = []
    for image in property.images:
        images.append({"property_id": property.id, "source_url": image.source_url})
        await _upsert_image(connection, property.id, image, now)
    places = []
    for place in property.nearby_places:
        keys = {"property_id": property.id, "name": place.name, "kind": place.kind}
        places.append(keys)
        await _upsert_row(
            connection,
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
        await _upsert_row(
            connection,
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
            await _remove_stale_children(connection, table, property.id, keys)


async def _remove_stale_children(
    connection: aiosqlite.Connection, table: str, property_id: int, current: list[dict[str, Any]]
) -> None:
    # Compare keys in Python rather than exceeding SQLite's parameter limit.
    if not current:
        await connection.execute(f"DELETE FROM {table} WHERE property_id = ?", (property_id,))
        return
    columns = tuple(current[0])
    keep = {tuple(keys[column] for column in columns) for keys in current}
    rows = await _fetch_all(
        connection,
        f"SELECT {', '.join(columns)} FROM {table} WHERE property_id = ?",
        (property_id,),
    )
    stale = [tuple(row) for row in rows if tuple(row) not in keep]
    where = " AND ".join(f"{column} = ?" for column in columns)
    await connection.executemany(f"DELETE FROM {table} WHERE {where}", stale)


async def _upsert_image(
    connection: aiosqlite.Connection, property_id: int, image: Image, now: str
) -> bool:
    return await _upsert_row(
        connection,
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


async def store_image(
    connection: aiosqlite.Connection,
    property_id: int,
    image: Image,
    content: bytes,
    content_type: str,
) -> bool:
    """Store image content; this association is output, never a retry checkpoint."""
    if not content:
        raise ValueError("Cannot store an empty image")
    sha256 = hashlib.sha256(content).hexdigest()
    now = _now()
    async with _operation(connection, write=True):
        if (
            await _fetch_one(connection, "SELECT * FROM properties WHERE id = ?", (property_id,))
            is None
        ):
            raise ValueError(f"Property {property_id} is missing from the archive")
        await _upsert_image(connection, property_id, image, now)
        existing = await _fetch_one(
            connection,
            "SELECT * FROM property_images WHERE property_id = ? AND source_url = ?",
            (property_id, image.source_url),
        )
        changed = existing["content_sha256"] != sha256
        await connection.execute(
            (
                "INSERT OR IGNORE INTO image_blobs (sha256, content, byte_length, content_type, "
                "created_at) VALUES (?, ?, ?, ?, ?)"
            ),
            (sha256, content, len(content), content_type, now),
        )
        await _upsert_row(
            connection,
            "property_images",
            {"property_id": property_id, "source_url": image.source_url},
            {"content_sha256": sha256},
            now,
        )
    return changed


async def _register_profile(
    connection: aiosqlite.Connection,
    profile_key: str,
    criteria: str,
    model: str,
    backend: str = "codex",
) -> None:
    await connection.execute(
        (
            "INSERT OR IGNORE INTO review_profiles (profile_key, criteria, model, backend, "
            "created_at) VALUES (?, ?, ?, ?, ?)"
        ),
        (profile_key, criteria, model, backend, _now()),
    )


async def has_review(connection: aiosqlite.Connection, property_id: int) -> bool:
    async with _operation(connection):
        return (
            await _fetch_one(
                connection, "SELECT 1 FROM property_reviews WHERE property_id = ?", (property_id,)
            )
            is not None
        )


async def get_evidence(connection: aiosqlite.Connection, property_id: int) -> ReviewSnapshot:
    async with _operation(connection):
        return await load_evidence(connection, property_id)


async def save_review(connection: aiosqlite.Connection, config, assessment) -> int:
    async with _operation(connection, write=True):
        await _register_profile(
            connection, config.profile_key, config.criteria, config.model, config.backend
        )
        facts = assessment.facts
        await connection.execute(
            (
                "INSERT INTO property_reviews (profile_key, property_id, fingerprint, result, "
                "processed_at, reviewed_title, reviewed_url, reviewed_address_display, "
                "reviewed_rent_pcm_pence, reviewed_bedrooms) VALUES (?, ?, ?, jsonb(?), ?, ?, ?, ?, ?, "
                "?) ON CONFLICT(profile_key, property_id, fingerprint) DO NOTHING"
            ),
            (
                config.profile_key,
                assessment.property_id,
                assessment.fingerprint,
                assessment.result,
                _now(),
                facts["title"],
                facts["url"],
                facts["address_display"],
                facts["rent_pcm_pence"],
                facts["bedrooms"],
            ),
        )
        review_id = (
            await _fetch_one(
                connection,
                "SELECT id FROM property_reviews WHERE profile_key = ? AND property_id = ? AND fingerprint = ?",
                (config.profile_key, assessment.property_id, assessment.fingerprint),
            )
        )[0]
        await connection.executemany(
            (
                "INSERT OR IGNORE INTO review_images (review_id, position, source_position, sha256, "
                "source_url, kind, caption) VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            [
                (
                    review_id,
                    index,
                    image["position"],
                    image["content_sha256"],
                    image["source_url"],
                    image["kind"],
                    image["caption"],
                )
                for index, image in enumerate(assessment.gallery)
            ],
        )
        result = json.loads(assessment.result)
        await connection.execute(
            "DELETE FROM notification_outbox WHERE property_id = ?", (assessment.property_id,)
        )
        if result["decision"] in {"pass", "uncertain"} and facts.get("is_live") != 0:
            payload = {
                "review_id": review_id,
                "property_id": assessment.property_id,
                **{
                    name: facts[name]
                    for name in ("title", "url", "address_display", "rent_pcm_pence", "bedrooms")
                },
                "property": facts,
                "result": result,
            }
            await connection.execute(
                "INSERT INTO notification_outbox (property_id, review_id, payload) VALUES (?, ?, jsonb(?))",
                (assessment.property_id, review_id, json.dumps(payload, allow_nan=False)),
            )
        return review_id


async def _email_review(connection: aiosqlite.Connection, review_id: int) -> aiosqlite.Row | None:
    return await _fetch_one(
        connection,
        "SELECT review_id, property_id FROM notification_outbox WHERE review_id = ?",
        (review_id,),
    )


async def pending_notifications(
    connection: aiosqlite.Connection, recipient: str
) -> list[dict[str, Any]]:
    """Read prepared review outputs not yet reserved for this recipient."""
    async with _operation(connection):
        return [
            json.loads(row[0])
            for row in await _fetch_all(
                connection,
                (
                    "SELECT json(o.payload) FROM notification_outbox o WHERE NOT EXISTS (SELECT 1 FROM "
                    "email_batch_items i WHERE i.recipient = ? AND i.property_id = o.property_id) ORDER BY "
                    "o.property_id"
                ),
                (recipient.casefold(),),
            )
        ]


async def pending_email_batch(
    connection: aiosqlite.Connection, recipient: str
) -> dict[str, Any] | None:
    """Return unresolved state; only pending/failed bodies can safely be retried.

    The serial notification queue means a leftover sending row belongs to an
    interrupted attempt. The provider may have accepted it, so quarantine it
    instead of automatically sending it again.
    """
    recipient = recipient.casefold()
    async with _operation(connection, write=True):
        await connection.execute(
            "UPDATE email_batches SET status = 'unknown', error = ? WHERE recipient = ? AND status = 'sending'",
            (
                "Previous sender stopped before confirming provider acceptance; reconcile manually",
                recipient,
            ),
        )
        row = await _fetch_one(
            connection,
            (
                "SELECT * FROM email_batches WHERE recipient = ? AND status IN ('pending', 'failed', "
                "'unknown') ORDER BY (status = 'unknown') DESC, id LIMIT 1"
            ),
            (recipient,),
        )
        if row is None:
            return None
        return dict(row)


async def create_email_batch(
    connection: aiosqlite.Connection,
    recipient: str,
    review_ids: list[int],
    subject: str,
    html_body: str,
    text_body: str,
) -> dict[str, Any]:
    recipient = recipient.casefold()
    if not review_ids or len(set(review_ids)) != len(review_ids):
        raise ValueError("An email batch requires distinct notifiable reviews")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in subject)
    ):
        raise ValueError("Email subject must be a nonempty single line")
    if not isinstance(html_body, str) or not isinstance(text_body, str):
        raise TypeError("Email batch bodies must be strings")
    async with _operation(connection, write=True):
        property_ids = []
        for review_id in review_ids:
            reviewed = await _email_review(connection, review_id)
            if reviewed is None:
                raise ValueError("An email batch contains an ineligible review")
            property_ids.append(reviewed["property_id"])
        if len(set(property_ids)) != len(property_ids):
            raise ValueError("An email batch requires distinct property IDs")
        cursor = await connection.execute(
            (
                "INSERT INTO email_batches (recipient, sender, subject, html_body, text_body, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (recipient, SENDER_EMAIL, subject, html_body, text_body, _now()),
        )
        await connection.executemany(
            "INSERT INTO email_batch_items (batch_id, recipient, property_id, review_id) VALUES (?, ?, ?, ?)",
            [
                (cursor.lastrowid, recipient, property_id, review_id)
                for property_id, review_id in zip(property_ids, review_ids, strict=True)
            ],
        )
        return dict(
            await _fetch_one(
                connection, "SELECT * FROM email_batches WHERE id = ?", (cursor.lastrowid,)
            )
        )


async def mark_email_sending(connection: aiosqlite.Connection, batch_id: int) -> bool:
    async with _operation(connection, write=True):
        return (
            await connection.execute(
                (
                    "UPDATE email_batches SET status = 'sending', last_attempt_at = ?, attempts = attempts "
                    "+ 1, error = NULL WHERE id = ? AND status IN ('pending', 'failed')"
                ),
                (_now(), batch_id),
            )
        ).rowcount == 1


async def finish_email_batch(
    connection: aiosqlite.Connection, batch_id: int, provider_message_id: str | None
) -> None:
    async with _operation(connection, write=True):
        cursor = await connection.execute(
            (
                "UPDATE email_batches SET status = 'sent', sent_at = ?, provider_message_id = ?, error "
                "= NULL WHERE id = ? AND status = 'sending'"
            ),
            (_now(), provider_message_id, batch_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Only a sending email batch can be confirmed sent")


async def fail_email_batch(
    connection: aiosqlite.Connection, batch_id: int, error: str, indeterminate: bool = False
) -> None:
    async with _operation(connection, write=True):
        await connection.execute(
            (
                "UPDATE email_batches SET status = ?, error = ? WHERE id = ? AND status IN ('pending', "
                "'sending', 'failed')"
            ),
            ("unknown" if indeterminate else "failed", error, batch_id),
        )
