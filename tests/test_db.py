import json
import sqlite3
from dataclasses import replace

import pytest

from openrent.db import SCHEMA_VERSION, Database
from openrent.models import Candidate, Feature, Image, MediaLink, NearbyPlace, Property


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "listings.sqlite") as database:
        yield database


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


def test_partial_import_preserves_existing_facts_and_children(db, property):
    db.upsert_property(property)
    db.store_image(property.id, property.images[0], b"some image", "image/jpeg", etag='"version1"')
    db.upsert_property(
        Property(
            id=property.id,
            url=property.url,
            extra_metadata={"irregular": {"new_fact": "new", "source": None}},
            features=[Feature("pets", "Pets allowed", "Tenant preferences", None)],
        )
    )

    row = db.get_property(property.id)
    assert row["rent_pcm_pence"] == 185000
    assert row["description"] == "A spacious flat."
    assert row["pets_allowed"] == 0
    assert json.loads(row["extra_metadata_json"]) == {
        "irregular": {"source": "example", "new_fact": "new"}
    }
    assert db.counts()["property_features"] == 4
    assert db.counts()["nearby_places"] == db.counts()["media_links"] == 1
    assert db.image_downloaded(property.id, property.images[0].source_url)
    feature = db.connection.execute(
        "SELECT * FROM property_features WHERE feature_key = 'pets'"
    ).fetchone()
    assert feature["value_type"] == "boolean"
    assert feature["value_boolean"] == 0


def test_complete_snapshot_removes_stale_associations_but_retains_blobs(db, property):
    db.upsert_property(property)
    db.store_image(property.id, property.images[0], b"some image", "image/jpeg")
    replacement = replace(
        property,
        rent_pcm_pence=190000,
        features=[],
        images=[],
        nearby_places=[],
        media_links=[],
        extra_metadata={},
    )
    db.upsert_property(replacement)

    counts = db.counts()
    assert counts["properties"] == counts["image_blobs"] == 1
    assert counts["property_images"] == counts["property_features"] == 0
    assert counts["nearby_places"] == counts["media_links"] == 0
    assert db.get_property(property.id)["rent_pcm_pence"] == 190000
    assert db.get_property(property.id)["extra_metadata_json"] == "{}"
    assert not db.image_downloaded(property.id, property.images[0].source_url)


def test_failed_images_are_retryable_without_losing_a_successful_download(db, property):
    db.upsert_property(property)
    url = property.images[0].source_url
    db.record_image_error(property.id, url, "HTTP 503")
    db.record_image_error(property.id, url, "HTTP 503")
    assert db.counts()["failed_images"] == 1
    assert not db.image_downloaded(property.id, url)
    db.store_image(property.id, property.images[0], b"successful download", "image/jpeg")
    assert db.image_downloaded(property.id, url)
    assert db.connection.execute("SELECT last_error FROM property_images").fetchone()[0] is None
    db.record_image_error(property.id, url, "HTTP 503 later")
    assert db.image_downloaded(property.id, url)
    assert db.counts()["failed_images"] == 0


def test_incomplete_search_never_deactivates_previous_matches(db, property):
    search_id = db.upsert_search({"maxRent": "2000"}, "London")
    second = replace(property, id=54321)
    db.record_match(search_id, Candidate(property, commute_minutes=15))
    db.record_match(search_id, Candidate(second, commute_minutes=19))
    db.finish_search(search_id, [property.id], complete=False)
    assert db.connection.execute("SELECT sum(active) FROM search_matches").fetchone()[0] == 2

    db.finish_search(search_id, [property.id], complete=True)
    active = dict(db.connection.execute("SELECT property_id, active FROM search_matches"))
    assert active == {property.id: 1, second.id: 0}
    assert db.get_property(second.id)["is_live"] is None
    db.record_match(search_id, Candidate(second))
    assert db.connection.execute("SELECT sum(active) FROM search_matches").fetchone()[0] == 2
    assert (
        db.connection.execute(
            "SELECT commute_minutes FROM search_matches WHERE property_id = ?", (second.id,)
        ).fetchone()[0]
        == 19
    )
    db.finish_search(search_id, [], complete=True)
    assert db.connection.execute("SELECT sum(active) FROM search_matches").fetchone()[0] == 0


def test_starting_search_resets_completion_flag_without_erasing_last_success(db):
    search_id = db.upsert_search({"maxRent": "2000"}, "London")
    db.finish_search(search_id, [], complete=True)
    completed = db.connection.execute("SELECT * FROM searches").fetchone()
    assert completed["last_search_complete"] == 1
    assert completed["last_completed_at"] is not None

    assert db.upsert_search({"maxRent": "2000"}, "London") == search_id
    interrupted = db.connection.execute("SELECT * FROM searches").fetchone()
    assert interrupted["last_search_complete"] == 0
    assert interrupted["last_completed_at"] == completed["last_completed_at"]
    assert db.counts()["searches"] == 1


def test_invalid_child_rolls_back_entire_property_update(db, property):
    db.upsert_property(property)
    original_counts = db.counts()
    invalid = replace(
        property,
        rent_pcm_pence=999999,
        images=[Image("https://images.openrent.co.uk/bad.jpg", width=-1)],
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_property(invalid)
    assert db.get_property(property.id)["rent_pcm_pence"] == 185000
    assert db.counts() == original_counts


def test_schema_version_foreign_keys_and_reopening_archive(tmp_path, property):
    path = tmp_path / "nested" / "archive.sqlite"
    with Database(path) as db:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.upsert_property(property)
        with pytest.raises(sqlite3.IntegrityError):
            db.store_image(
                999, Image("https://images.openrent.co.uk/no-property.jpg"), b"image", "image/jpeg"
            )
    with Database(path) as db:
        assert db.upsert_property(property) is False
        assert db.counts()["properties"] == 1
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(ValueError, match="newer than supported"):
        Database(path)
