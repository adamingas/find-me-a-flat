import asyncio
import json
import sqlite3
from dataclasses import replace

import aiosqlite
import pytest
import pytest_asyncio
from archive_helpers import image_content, table_counts

from openrent import db as database
from openrent.models import Feature, Image, MediaLink, NearbyPlace, Property


@pytest_asyncio.fixture
async def db(tmp_path):
    async with database.connect(tmp_path / "listings.sqlite") as connection:
        yield connection


@pytest.fixture
def property():
    return Property(
        id=12345,
        url="https://www.openrent.co.uk/property-to-rent/london/12345",
        title="2 Bed Flat",
        description="A spacious flat.",
        address_display="Example Road, London, SW1A",
        locality="London",
        postcode="SW1A 1AA",
        latitude=51.5,
        longitude=-0.12,
        bedrooms=2,
        bathrooms=1,
        rent_pcm_pence=185000,
        rent_weekly_pence=42692,
        deposit_pence=213461,
        pets_allowed=False,
        available_from="2026-10-01",
        detail_complete=True,
        extra_metadata={"irregular": {"source": "example"}},
        features=[
            Feature("pets", "Pets allowed", "Tenant preferences", False),
            Feature("floor_area", "Floor area", "Other", 74.5, "m²"),
            Feature("floor", "Floor", "Other", 2),
            Feature("council_band", "Council tax band", "Other", "C"),
        ],
        images=[Image("https://images.openrent.co.uk/a.jpg", caption="Living room")],
        nearby_places=[NearbyPlace("Example Station", walking_minutes=7, distance_km=0.5)],
        media_links=[MediaLink("https://www.youtube.com/watch?v=example")],
    )


@pytest.mark.asyncio
async def test_partial_import_preserves_existing_facts_and_children(db, property):
    property = replace(
        property,
        openrent_commute_minutes=22.5,
        search_target="Victoria",
        search_target_latitude=51.495812,
        search_target_longitude=-0.143826,
    )
    await database.upsert_property(db, property)
    await database.store_image(db, property.id, property.images[0], b"some image", "image/jpeg")
    await database.upsert_property(
        db,
        Property(
            id=property.id,
            url=property.url,
            extra_metadata={"irregular": {"new_fact": "new", "source": None}},
            features=[Feature("pets", "Pets allowed", "Tenant preferences", None)],
        ),
    )

    row = await database.get_property(db, property.id)
    assert row["rent_pcm_pence"] == 185000
    assert row["description"] == "A spacious flat."
    assert row["pets_allowed"] == 0
    assert row["openrent_commute_minutes"] == 22.5
    assert row["search_target"] == "Victoria"
    assert row["search_target_latitude"] == 51.495812
    assert row["search_target_longitude"] == -0.143826
    assert json.loads(row["extra_metadata_json"]) == {
        "irregular": {"source": "example", "new_fact": "new"}
    }
    assert (await table_counts(db))["property_features"] == 4
    assert (await table_counts(db))["nearby_places"] == (await table_counts(db))["media_links"] == 1
    assert await image_content(db, property.id, property.images[0].source_url) == b"some image"
    feature = await database.fetch_one(
        db, "SELECT * FROM property_features WHERE feature_key = 'pets'"
    )
    assert feature["value_type"] == "boolean"
    assert feature["value_boolean"] == 0


@pytest.mark.asyncio
async def test_complete_snapshot_removes_stale_associations_but_retains_blobs(db, property):
    await database.upsert_property(db, property)
    await database.store_image(db, property.id, property.images[0], b"some image", "image/jpeg")
    replacement = replace(
        property,
        rent_pcm_pence=190000,
        features=[],
        images=[],
        nearby_places=[],
        media_links=[],
        extra_metadata={},
    )
    await database.upsert_property(db, replacement)

    counts = await table_counts(db)
    assert counts["properties"] == counts["image_blobs"] == 1
    assert counts["property_images"] == counts["property_features"] == 0
    assert counts["nearby_places"] == counts["media_links"] == 0
    assert (await database.get_property(db, property.id))["rent_pcm_pence"] == 190000
    assert (await database.get_property(db, property.id))["extra_metadata_json"] == "{}"
    assert await image_content(db, property.id, property.images[0].source_url) is None


@pytest.mark.asyncio
async def test_invalid_child_rolls_back_entire_property_update(db, property):
    await database.upsert_property(db, property)
    original_counts = await table_counts(db)
    invalid = replace(
        property,
        rent_pcm_pence=999999,
        images=[Image("https://images.openrent.co.uk/bad.jpg", width=-1)],
    )
    with pytest.raises(sqlite3.IntegrityError):
        await database.upsert_property(db, invalid)
    assert (await database.get_property(db, property.id))["rent_pcm_pence"] == 185000
    assert await table_counts(db) == original_counts


@pytest.mark.asyncio
async def test_foreign_keys_and_reopening_archive(tmp_path, property):
    path = tmp_path / "nested" / "archive.sqlite"
    async with database.connect(path) as db:
        assert (await database.fetch_one(db, "PRAGMA foreign_keys"))[0] == 1
        await database.upsert_property(db, property)
        with pytest.raises(ValueError, match="missing"):
            await database.store_image(
                db,
                999,
                Image("https://images.openrent.co.uk/no-property.jpg"),
                b"image",
                "image/jpeg",
            )
        assert (await table_counts(db))["properties"] == 1
    async with database.connect(path) as db:
        assert await database.upsert_property(db, property) is False
        assert (await table_counts(db))["properties"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_schema", [True, False])
async def test_misplaced_openrent_minutes_move_to_properties_without_losing_facts(
    tmp_path, property, legacy_schema
):
    path = tmp_path / "archive.sqlite"
    fields = (
        "openrent_commute_minutes",
        "search_target",
        "search_target_latitude",
        "search_target_longitude",
    )
    expected = (0, "Victoria", 51.495812, -0.143826)
    if not legacy_schema:
        expected = (22.5, "Pimlico", 51.489, -0.133)
        property = replace(property, **dict(zip(fields, expected)))
    async with database.connect(path) as db:
        await database.upsert_property(db, property)
        await database.store_amenity(db, property.id, "gym", json.dumps({"gyms": []}))
        await database.store_amenity(
            db,
            property.id,
            "openrent_commute",
            json.dumps(
                {
                    "source": "OpenRent",
                    "duration_minutes": 0,
                    "target": "Victoria",
                    "target_latitude": 51.495812,
                    "target_longitude": -0.143826,
                }
            ),
        )
        before = await table_counts(db)
    if legacy_schema:
        async with aiosqlite.connect(path) as connection:
            for field in fields:
                await connection.execute(f"ALTER TABLE properties DROP COLUMN {field}")
            await connection.commit()
    for _ in range(2):
        async with database.connect(path) as db:
            row = await database.get_property(db, property.id)
            assert tuple(row[field] for field in fields) == expected
            assert await database.get_amenities(db, property.id) == {"gym": {"gyms": []}}
            assert await table_counts(db) == before
            assert not await database.fetch_all(db, "PRAGMA foreign_key_check")


@pytest.mark.asyncio
async def test_parallel_writers_merge_overlapping_ids_with_atomic_metadata_and_images(tmp_path):
    path = tmp_path / "shared.sqlite"

    async def ingest(db, worker):
        for property_id in [*range(1, 31), 100 + worker]:
            image = Image(f"https://images.openrent.co.uk/{property_id}.jpg")
            prop = Property(
                property_id,
                f"https://www.openrent.co.uk/{property_id}",
                rent_pcm_pence=100000 + worker,
                description=str(worker),
                features=[Feature("worker", "Worker", value=worker)],
                images=[image],
                detail_complete=True,
            )
            await database.upsert_property(db, prop)
            await database.store_image(db, property_id, image, b"shared image bytes", "image/jpeg")

    async with database.connect(path) as db:
        await asyncio.gather(*(ingest(db, worker) for worker in range(4)))
        counts = await table_counts(db)
        assert (
            counts["properties"] == counts["property_features"] == counts["property_images"] == 34
        )
        assert counts["image_blobs"] == 1
        for row in await database.query(db, "SELECT * FROM properties"):
            worker = int(row["description"])
            assert row["rent_pcm_pence"] == 100000 + worker
            feature = (
                await database.fetch_one(
                    db,
                    "SELECT value_integer FROM property_features WHERE property_id = ?",
                    (row["id"],),
                )
            )[0]
            assert feature == worker
        assert not await database.fetch_all(db, "PRAGMA foreign_key_check")
