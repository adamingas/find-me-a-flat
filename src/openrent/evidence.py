"""Read a complete archived dossier for the model adapter; no network or queue decisions."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

_OBSERVATION_COLUMNS = {"first_seen_at", "last_seen_at", "updated_at"}


@dataclass(frozen=True)
class ArchivedImage:
    sha256: str
    content_type: str
    content: bytes
    caption: str | None
    source_url: str
    kind: str
    position: int


@dataclass(frozen=True)
class ReviewSnapshot:
    property_id: int
    fingerprint: str
    data: dict[str, Any]
    images: tuple[ArchivedImage, ...]


async def load_evidence(connection, property_id: int) -> ReviewSnapshot:
    row = await _fetch_one(connection, "SELECT * FROM properties WHERE id = ?", (property_id,))
    if row is None:
        raise ValueError(f"Property {property_id} is missing from the archive")
    data = dict(row)
    data["extra_metadata"] = json.loads(data.pop("extra_metadata_json"))
    data["amenities"] = {
        item["type"]: json.loads(item["data"])
        for item in await connection.execute_fetchall(
            "SELECT type, json(data) AS data FROM amenities WHERE property_id = ? ORDER BY type",
            (property_id,),
        )
    }
    for name, ordering in (
        ("property_features", "section, feature_key"),
        ("nearby_places", "kind, name"),
        ("media_links", "kind, url"),
    ):
        data[name.removeprefix("property_")] = [
            dict(item)
            for item in await connection.execute_fetchall(
                f"SELECT * FROM {name} WHERE property_id = ? ORDER BY {ordering}", (property_id,)
            )
        ]
    gallery = await connection.execute_fetchall(
        (
            "SELECT i.*, b.content_type, b.byte_length, b.content FROM property_images i LEFT JOIN "
            "image_blobs b ON b.sha256 = i.content_sha256 WHERE i.property_id = ? ORDER BY "
            "i.position, i.source_url"
        ),
        (property_id,),
    )
    if any(image["content"] is None for image in gallery):
        raise ValueError(f"Property {property_id} has missing image content")
    data["images"] = [
        {name: image[name] for name in dict(image) if name != "content"} for image in gallery
    ]
    # Observation timestamps and HTML backups do not change the review evidence.
    fingerprint_data = {
        name: value
        for name, value in data.items()
        if name not in _OBSERVATION_COLUMNS | {"source_html", "description_html"}
    }
    for name in ("features", "nearby_places", "media_links", "images"):
        fingerprint_data[name] = [
            {key: value for key, value in item.items() if key not in _OBSERVATION_COLUMNS}
            for item in data[name]
        ]
    canonical = json.dumps(
        fingerprint_data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    images = tuple(
        ArchivedImage(
            sha256=image["content_sha256"],
            content_type=image["content_type"],
            content=image["content"],
            caption=image["caption"],
            source_url=image["source_url"],
            kind=image["kind"],
            position=image["position"],
        )
        for image in gallery
    )
    return ReviewSnapshot(property_id, fingerprint, data, images)


async def _fetch_one(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()
