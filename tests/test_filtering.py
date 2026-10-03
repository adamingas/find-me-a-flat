"""Post-fetch pruning checks boundaries, scope, shared media, and atomic deletion."""

import hashlib
from dataclasses import replace

import pytest

from openrent import filtering
from openrent.db import Database
from openrent.models import Feature, Image, MediaLink, NearbyPlace, Property
from openrent.review_db import ReviewDatabase


def listing(property_id, **facts):
    facts.setdefault("epc_rating", "C")
    return Property(
        property_id,
        f"https://www.openrent.co.uk/{property_id}",
        is_live=True,
        detail_complete=True,
        **facts,
    )


def test_tube_rule_is_inclusive_read_only_and_scoped_to_input_ids():
    stations = [
        [NearbyPlace("Boundary", "underground", 11)],
        [NearbyPlace("Too far", "underground", 12)],
        [NearbyPlace("Train", "national_rail", 1), NearbyPlace("Overground", "overground", 1)],
        [NearbyPlace("Unknown", "underground")],
        [],
        [NearbyPlace("Far", "underground", 15), NearbyPlace("Near", "underground", 3)],
        [NearbyPlace("At station", "underground", 0)],
        [NearbyPlace("Outside scan", "underground", 9)],
    ]
    with Database(":memory:") as db:
        for property_id, nearby in enumerate(stations, start=1):
            db.upsert_property(listing(property_id, nearby_places=nearby))
        before = tuple(db.connection.iterdump())
        assert filtering.filter_property_ids(db.connection, [7, 6, 5, 4, 3, 2, 1]) == [7, 6, 1]
        assert filtering.filter_property_ids(db.connection, []) == []
        # More IDs than a traditional SQLite bind limit, plus duplicate inputs.
        assert filtering.filter_property_ids(db.connection, [7, 7, *range(1000, 2600), 1]) == [7, 1]
        assert tuple(db.connection.iterdump()) == before


def test_epc_rule_requires_a_b_or_c_as_well_as_a_qualifying_tube():
    ratings = [
        ("A", True),
        ("B", True),
        ("C", True),
        ("D", False),
        ("E", False),
        ("F", False),
        ("G", False),
        (None, False),
        ("", False),
        ("N/A", False),
        ("A+", False),
        (" c ", True),
        ("b", True),
    ]
    with Database(":memory:") as db:
        ids, expected = [], []
        for property_id, (rating, passes) in enumerate(ratings, start=1):
            ids.append(property_id)
            db.upsert_property(
                listing(
                    property_id,
                    epc_rating=rating,
                    nearby_places=[NearbyPlace("Tube", "underground", 11)],
                )
            )
            if passes:
                expected.append(property_id)
        # A good EPC alone cannot compensate for a failed transport condition.
        db.upsert_property(
            listing(100, epc_rating="A", nearby_places=[NearbyPlace("Tube", "underground", 12)])
        )
        db.upsert_property(
            listing(101, epc_rating="B", nearby_places=[NearbyPlace("Train", "national_rail", 1)])
        )
        db.upsert_property(listing(102, epc_rating="C"))
        assert filtering.filter_property_ids(db.connection, [*ids, 100, 101, 102]) == expected


def test_deletion_cascades_reviews_and_preserves_shared_and_review_only_images():
    profile = hashlib.sha256(b"criteria").hexdigest()
    judgement = {
        "decision": "pass",
        "summary": "Example judgement",
        "result": {
            "decision": "pass",
            "summary": "Example judgement",
            "criteria": [{"criterion": "example", "met": True, "evidence": "Example"}],
        },
    }
    with ReviewDatabase(":memory:") as db:
        db.register_profile(profile, "criteria", "example-model")
        properties = []
        for property_id, contents in (
            (1, [b"reject-only", b"shared", b"protected"]),
            (2, [b"shared", b"protected"]),
        ):
            images = [
                Image(f"https://images.openrent.co.uk/{property_id}/{position}.jpg", position)
                for position in range(len(contents))
            ]
            prop = listing(
                property_id,
                images=images,
                features=[Feature("floor", "Floor", value=2)],
                nearby_places=[NearbyPlace("Tube", "underground", 11)],
                media_links=[MediaLink(f"https://example.com/{property_id}")],
            )
            properties.append(prop)
            db.upsert_property(prop)
            for image, content in zip(images, contents, strict=True):
                db.store_image(property_id, image, content, "image/jpeg")
        for prop in properties:
            review_id, _ = db.claim_review(profile, prop.id)
            assert db.complete_review(review_id, judgement)

        # The rejected listing's old gallery now exists only in its review.
        new_image = Image("https://images.openrent.co.uk/new.jpg")
        db.upsert_property(replace(properties[0], images=[new_image]))
        db.store_image(1, new_image, b"new", "image/jpeg")
        # The surviving listing's review alone references the protected BLOB.
        db.upsert_property(replace(properties[1], images=properties[1].images[:1]))

        assert db.delete_properties([1]) == 1
        assert db.get_property(1) is None
        for table in (
            "property_images",
            "property_features",
            "nearby_places",
            "media_links",
            "review.property_reviews",
        ):
            assert (
                db.connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE property_id = 1"
                ).fetchone()[0]
                == 0
            )
        assert (
            db.connection.execute(
                "SELECT COUNT(*) FROM review.property_reviews WHERE result IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert db.connection.execute("SELECT COUNT(*) FROM review.review_images").fetchone()[0] == 2
        assert {row[0] for row in db.connection.execute("SELECT content FROM image_blobs")} == {
            b"shared",
            b"protected",
        }
        assert (
            db.connection.execute("SELECT COUNT(*) FROM review.review_profiles").fetchone()[0] == 1
        )
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert db.delete_properties([1]) == 0
        assert db.delete_properties([2, 2, 999]) == 1
        assert db.counts()["image_blobs"] == 0
        assert db.connection.execute("SELECT COUNT(*) FROM review.review_images").fetchone()[0] == 0


def test_blob_cleanup_failure_rolls_back_property_and_children():
    import sqlite3

    with Database(":memory:") as db:
        image = Image("https://images.openrent.co.uk/1.jpg")
        db.upsert_property(
            listing(1, images=[image], nearby_places=[NearbyPlace("Tube", "underground", 12)])
        )
        db.store_image(1, image, b"bytes", "image/jpeg")
        db.connection.execute(
            "CREATE TRIGGER fail_cleanup BEFORE DELETE ON image_blobs "
            "BEGIN SELECT RAISE(ABORT, 'cleanup failure'); END"
        )
        before = tuple(db.connection.iterdump())
        with pytest.raises(sqlite3.IntegrityError, match="cleanup failure"):
            db.filter_properties([1])
        assert tuple(db.connection.iterdump()) == before
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()


def test_filter_failure_or_out_of_scope_result_does_not_delete_saved_data(monkeypatch):
    with Database(":memory:") as db:
        db.upsert_property(listing(1))
        db.upsert_property(listing(2))
        before = tuple(db.connection.iterdump())

        def fail(*args):
            raise ValueError("filter failure")

        monkeypatch.setattr(filtering, "filter_property_ids", fail)
        with pytest.raises(ValueError, match="filter failure"):
            db.filter_properties([1])
        assert tuple(db.connection.iterdump()) == before
        monkeypatch.setattr(filtering, "filter_property_ids", lambda *args: [2])
        with pytest.raises(ValueError, match="outside the current scan"):
            db.filter_properties([1])
        assert tuple(db.connection.iterdump()) == before
