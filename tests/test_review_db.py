import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from openrent.db import REVIEW_SCHEMA_VERSION, SCHEMA_VERSION, Database, review_path_for
from openrent.models import Feature, Image, MediaLink, NearbyPlace, Property
from openrent.review_db import AsyncReviewDatabase, ReviewDatabase

PROFILE = hashlib.sha256(b"bright flat").hexdigest()
OTHER_PROFILE = hashlib.sha256(b"different criteria").hexdigest()
RESULT = {
    "decision": "pass",
    "summary": "Bright living room; meets the stated criteria.",
    "criteria": [
        {
            "criterion": "bright",
            "met": True,
            "is_deal_breaking": False,
            "evidence": "Two large windows.",
            "additional_model_fact": {"windows": 2},
        }
    ],
    "images_examined": [1],
    "has_deal_breaking_criteria": False,
}
JUDGEMENT = {"decision": RESULT["decision"], "summary": RESULT["summary"], "result": RESULT}


def listing(property_id=1):
    return Property(
        id=property_id,
        url=f"https://www.openrent.co.uk/property-to-rent/london/{property_id}",
        title="A bright flat",
        description="Two large windows",
        address_display="Example Road, London",
        rent_pcm_pence=200000,
        bedrooms=1,
        is_live=True,
        features=[Feature("floor", "Floor", value=2)],
        nearby_places=[NearbyPlace("Victoria", walking_minutes=8)],
        media_links=[MediaLink("https://example.com/video")],
        images=[Image("https://images.openrent.co.uk/living.jpg", caption="Living room")],
        extra_metadata={"irregular": {"size": 44}},
        detail_complete=True,
    )


def store_ready(db, property):
    db.upsert_property(property)
    for image in property.images:
        db.store_image(property.id, image, b"original image", "image/jpeg")


def test_v1_archive_migrates_without_losing_facts_or_image_bytes(tmp_path):
    path = tmp_path / "archive.sqlite"
    schema = Path(__file__).parents[1] / "src" / "openrent" / "schema.sql"
    old_schema = schema.read_text().replace("PRAGMA user_version = 6;", "PRAGMA user_version = 1;")
    with sqlite3.connect(path) as connection:
        connection.executescript(old_schema)
        connection.execute(
            "INSERT INTO properties (id, url, rent_pcm_pence, first_seen_at, last_seen_at, updated_at) "
            "VALUES (1, 'https://www.openrent.co.uk/property-to-rent/london/1', 200000, 'old', 'old', 'old')"
        )
        content = b"original image"
        sha256 = hashlib.sha256(content).hexdigest()
        connection.execute(
            "INSERT INTO image_blobs VALUES (?, ?, ?, 'image/jpeg', 'old')",
            (sha256, content, len(content)),
        )
        connection.execute(
            "INSERT INTO property_images "
            "(property_id, source_url, content_sha256, download_status, first_seen_at, "
            "last_seen_at, updated_at) VALUES (1, ?, ?, 'downloaded', 'old', 'old', 'old')",
            (listing().images[0].source_url, sha256),
        )
    # Opening the archive performs the migration; old tables keep their shape.
    with ReviewDatabase(path) as db:
        assert db.get_property(1)["first_seen_at"] == "old"
        assert db.image_downloaded(1, listing().images[0].source_url)
        store_ready(db, listing())
        db.register_profile(PROFILE, "bright flat", "example-model")
        claim = db.claim_review(PROFILE, 1)
        assert claim is not None
        review_id, snapshot = claim
        assert snapshot.images[0].content == b"original image"
        assert set(dict(db.get_property(1))) - {"extra_metadata_json"} <= set(snapshot.data)
        assert snapshot.data["extra_metadata"] == listing().extra_metadata
        assert snapshot.data["features"][0]["property_id"] == 1
        assert "downloaded_at" in snapshot.data["images"][0]
        assert db.complete_review(review_id, JUDGEMENT)
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            db.connection.execute("PRAGMA review.user_version").fetchone()[0]
            == REVIEW_SCHEMA_VERSION
        )
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert not db.connection.execute("PRAGMA review.foreign_key_check").fetchall()
        result = db.connection.execute(
            "SELECT typeof(result), json_valid(result,8), json(result), decision, summary "
            "FROM review.property_reviews",
        ).fetchone()
        assert result[0] == "blob" and result[1] == 1
        assert json.loads(result[2]) == RESULT
        assert (result[3], result[4]) == (RESULT["decision"], RESULT["summary"])
        columns = {
            row["name"]: row
            for row in db.connection.execute(
                "PRAGMA review.table_xinfo(property_reviews)",
            )
        }
        assert columns["decision"]["hidden"] == columns["summary"]["hidden"] == 2
        assert (
            db.review_path == review_path_for(path) == Path(str(path.resolve()) + ".review.sqlite")
        )
        assert db.review_path.is_file()
        assert db.counts()["review_images"] == db.counts()["reviews_complete"] == 1
    with Database(path) as db:
        assert db.get_property(1)["rent_pcm_pence"] == 200000
        assert (
            db.connection.execute("SELECT content FROM image_blobs").fetchone()[0]
            == b"original image"
        )
        assert (
            json.loads(
                db.connection.execute(
                    "SELECT json(result) FROM review.property_reviews",
                ).fetchone()[0]
            )
            == RESULT
        )
    with Database(path.with_suffix(".db")) as other:
        assert other.review_path != review_path_for(path)
        assert (
            other.connection.execute("SELECT count(*) FROM review.property_reviews").fetchone()[0]
            == 0
        )


def test_incomplete_or_inactive_items_wait_and_completed_ids_stay_processed_across_profiles():
    with ReviewDatabase(":memory:") as db:
        db.register_profile(PROFILE, "bright flat", "example-model")
        db.register_profile(OTHER_PROFILE, "different criteria", "example-model")
        property = listing()
        second_image = Image("https://images.openrent.co.uk/bedroom.jpg", position=1)
        property = replace(property, images=[*property.images, second_image])
        db.upsert_property(property)
        assert db.candidate_ids(PROFILE) == []
        assert db.unprocessed_ids() == []
        db.store_image(1, property.images[0], b"original image", "image/jpeg")
        assert db.claim_review(PROFILE, 1) is None
        assert db.unprocessed_ids() == []
        db.record_image_error(1, second_image.source_url, "503")
        assert db.candidate_ids(PROFILE) == []
        db.store_image(1, second_image, b"bedroom image", "image/jpeg")
        assert db.unprocessed_ids() == [1]
        # A downloaded association whose BLOB is missing is not ready either.
        original_sha = hashlib.sha256(b"original image").hexdigest()
        db.connection.execute("PRAGMA foreign_keys = OFF")
        with db.connection:
            db.connection.execute("DELETE FROM image_blobs WHERE sha256 = ?", (original_sha,))
        assert db.unprocessed_ids() == []
        db.store_image(1, property.images[0], b"original image", "image/jpeg")
        db.connection.execute("PRAGMA foreign_keys = ON")
        assert db.candidate_ids(PROFILE) == db.unprocessed_ids() == [1]
        review_id, snapshot = db.claim_review(PROFILE, 1)
        # Observation times and source markup are excluded from the fingerprint.
        db.upsert_property(
            replace(property, source_html="different page", description_html="<p>new</p>")
        )
        assert db.complete_review(review_id, JUDGEMENT)
        assert db.unprocessed_ids() == []
        db.upsert_property(replace(property, rent_pcm_pence=240000, description="Refurbished"))
        assert db.candidate_ids(PROFILE) == db.candidate_ids(OTHER_PROFILE) == []
        assert db.unprocessed_ids() == []
        assert db.claim_review(OTHER_PROFILE, 1) is None
        reviewed = db.connection.execute(
            "SELECT * FROM review.property_reviews WHERE id = ?", (review_id,)
        ).fetchone()
        assert reviewed["reviewed_rent_pcm_pence"] == snapshot.data["rent_pcm_pence"] == 200000
        assert reviewed["summary"] == JUDGEMENT["summary"]
        assert reviewed["status"] == "complete"
        assert (
            json.loads(
                db.connection.execute(
                    "SELECT json(result) FROM review.property_reviews WHERE id = ?",
                    (review_id,),
                ).fetchone()[0]
            )
            == RESULT
        )
        db.upsert_property(replace(property, is_live=False))
        assert db.candidate_ids(PROFILE) == []
        # A floorplan by itself is insufficient for judging photographs.
        store_ready(db, replace(listing(2), images=[replace(second_image, kind="floorplan")]))
        store_ready(db, replace(listing(3), is_live=False))
        db.upsert_property(replace(listing(4), images=[]))
        db.upsert_property(replace(listing(5), images=[], is_live=None))
        assert db.candidate_ids(PROFILE) == []
        assert db.unprocessed_ids() == []
        store_ready(db, listing(6))
        store_ready(db, listing(7))
        assert db.unprocessed_ids() == [6, 7]
        assert db.unprocessed_ids(limit=1) == [6]
        for limit in (0, -1, True, 1.5, "2"):
            with pytest.raises(ValueError, match="positive integer"):
                db.unprocessed_ids(limit)
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()


def test_changed_review_inputs_never_mark_processed_and_failures_can_be_retried():
    with ReviewDatabase(":memory:") as db:
        db.register_profile(PROFILE, "bright flat", "example-model")
        property = listing()
        store_ready(db, property)
        original_id, original = db.claim_review(PROFILE, 1)
        db.fail_review(original_id, "Codex failed")
        retry_id, retry = db.claim_review(PROFILE, 1)
        assert retry_id == original_id and retry.fingerprint == original.fingerprint
        for change in (
            {"rent_pcm_pence": 210000},
            {"description": "New description"},
            {"features": [Feature("floor", "Floor", value=5)]},
            {"nearby_places": [NearbyPlace("Pimlico", walking_minutes=2)]},
            {"extra_metadata": {"irregular": {"size": 55}}},
        ):
            current_id, _ = db.claim_review(PROFILE, 1)
            property = replace(property, **change)
            db.upsert_property(property)
            assert not db.complete_review(current_id, JUDGEMENT)
            assert db.candidate_ids(PROFILE) == [1]
            assert db.counts()["reviews_complete"] == 0
        current_id, _ = db.claim_review(PROFILE, 1)
        db.store_image(1, property.images[0], b"changed image", "image/jpeg")
        assert not db.complete_review(current_id, JUDGEMENT)
        final_id, final = db.claim_review(PROFILE, 1)
        assert final.images[0].content == b"changed image"
        assert db.complete_review(final_id, JUDGEMENT)
        db.fail_review(final_id, "Late failure cannot undo completion")
        assert db.candidate_ids(PROFILE) == []
        with pytest.raises(ValueError, match="not currently processing"):
            db.complete_review(final_id, JUDGEMENT)


def test_async_review_facade_uses_normal_archive_worker(tmp_path):
    async def run():
        async with AsyncReviewDatabase(tmp_path / "review.sqlite") as db:
            property = listing()
            await db.register_profile(PROFILE, "bright flat", "example-model")
            await db.upsert_property(property)
            assert await db.unprocessed_ids(limit=1) == []
            await db.store_image(1, property.images[0], b"photo", "image/jpeg")
            assert await db.unprocessed_ids(limit=1) == [1]
            assert await db.candidate_ids(PROFILE) == [1]
            review_id, snapshot = await db.claim_review(PROFILE, 1)
            assert snapshot.images[0].content == b"photo"
            assert await db.complete_review(review_id, JUDGEMENT)
            assert await db.candidate_ids(PROFILE) == []
            assert await db.unprocessed_ids() == []
            assert (await db.counts())["reviews_complete"] == 1
            uncertain_property = listing(2)
            await db.upsert_property(uncertain_property)
            await db.store_image(2, uncertain_property.images[0], b"uncertain photo", "image/jpeg")
            uncertain_id, _ = await db.claim_review(PROFILE, 2)
            uncertain_result = dict(RESULT, decision="uncertain")
            assert await db.complete_review(
                uncertain_id, dict(JUDGEMENT, decision="uncertain", result=uncertain_result)
            )
            recipient = "me@example.com"
            assert [item["review_id"] for item in await db.pending_notifications(recipient)] == [
                review_id,
                uncertain_id,
            ]
            batch = await db.create_email_batch(
                recipient, [review_id, uncertain_id], "Two flats", "HTML", "Text"
            )
            assert (await db.pending_email_batch(recipient))["id"] == batch["id"]
            assert await db.mark_email_sending(batch["id"])
            await db.fail_email_batch(batch["id"], "Definitive refusal")
            assert (await db.pending_email_batch(recipient))["status"] == "failed"
            assert await db.mark_email_sending(batch["id"])
            await db.finish_email_batch(batch["id"], None)
            assert await db.pending_email_batch(recipient) is None
            assert await db.pending_notifications(recipient) == []
            assert (await db.counts())["emails_sent"] == 1

    asyncio.run(run())


def test_email_batches_are_once_per_property_and_recipient_with_safe_retries():
    recipient = "Person <Person@Example.com>"
    with ReviewDatabase(":memory:") as db:
        db.register_profile(PROFILE, "bright flat", "example-model")
        ids = {}
        for property_id, decision in (
            (1, "pass"),
            (2, "reject"),
            (3, "pass"),
            (4, "uncertain"),
            (7, "pass"),
        ):
            property = listing(property_id)
            if property_id == 4:
                property = replace(
                    property,
                    images=[
                        *property.images,
                        Image(
                            "https://images.openrent.co.uk/floorplan.jpg",
                            position=1,
                            kind="floorplan",
                            caption="Floorplan",
                        ),
                    ],
                )
            store_ready(db, property)
            review_id, _ = db.claim_review(PROFILE, property_id)
            result = dict(RESULT, decision=decision)
            assert db.complete_review(review_id, dict(JUDGEMENT, decision=decision, result=result))
            ids[property_id] = review_id
        # Processing/errors and an older pass superseded by a completed reject
        # must not enter either selection or a manually constructed batch.
        for property_id in (5, 6):
            store_ready(db, listing(property_id))
            ids[property_id], _ = db.claim_review(PROFILE, property_id)
        db.fail_review(ids[6], "Review failed")
        db.upsert_property(replace(listing(7), description="Changed for a later review"))
        later_id, _ = db.claim_review(PROFILE, 7, once_per_property=False)
        rejected_result = dict(RESULT, decision="reject")
        assert db.complete_review(
            later_id, dict(JUDGEMENT, decision="reject", result=rejected_result)
        )
        db.upsert_property(replace(listing(3), is_live=False))
        db.upsert_property(
            replace(
                listing(1),
                title="Changed title",
                rent_pcm_pence=240000,
                images=[Image("https://images.openrent.co.uk/replacement.jpg")],
            )
        )
        db.upsert_property(
            replace(listing(4), images=[Image("https://images.openrent.co.uk/new.jpg")])
        )
        notifiable = db.pending_notifications(recipient)
        assert [record["property_id"] for record in notifiable] == [1, 4]
        assert notifiable[0]["title"] == notifiable[0]["property"]["title"] == "A bright flat"
        assert notifiable[0]["property"]["rent_pcm_pence"] == 200000
        assert notifiable[0]["property"]["nearby_places"][0]["name"] == "Victoria"
        assert notifiable[0]["property"]["features"][0]["value_integer"] == 2
        assert notifiable[0]["property"]["images"] == [
            {
                "source_url": listing(1).images[0].source_url,
                "position": 0,
                "kind": "photo",
                "caption": "Living room",
            }
        ]
        assert notifiable[0]["result"] == RESULT
        assert notifiable[1]["result"]["decision"] == "uncertain"
        assert notifiable[1]["property"]["images"] == [
            {
                "source_url": listing(4).images[0].source_url,
                "position": 0,
                "kind": "photo",
                "caption": "Living room",
            },
            {
                "source_url": "https://images.openrent.co.uk/floorplan.jpg",
                "position": 1,
                "kind": "floorplan",
                "caption": "Floorplan",
            },
        ]
        for property_id in (2, 3, 5, 6, 7):
            with pytest.raises(ValueError, match="ineligible review"):
                db.create_email_batch(recipient, [ids[property_id]], "Invalid", "html", "text")
        assert db.counts()["email_batches"] == 0
        batch = db.create_email_batch(
            recipient, [ids[1], ids[4]], "Two notifiable flats", "<p>HTML</p>", "Text"
        )
        assert batch["recipient"] == "person@example.com"
        assert batch["sender"] == "notifications@flats.spanashis.com"
        assert db.pending_notifications("person@example.com") == []
        with pytest.raises(sqlite3.IntegrityError):
            db.create_email_batch(recipient, [ids[1]], "Duplicate", "html", "text")
        assert db.counts()["email_batches"] == 1  # Duplicate reservation rolls back its new batch.
        assert db.mark_email_sending(batch["id"])
        db.fail_email_batch(batch["id"], "Provider refused before accepting")
        retry = db.pending_email_batch("PERSON@EXAMPLE.COM")
        assert (retry["id"], retry["subject"], retry["html_body"], retry["text_body"]) == (
            batch["id"],
            batch["subject"],
            batch["html_body"],
            batch["text_body"],
        )
        assert retry["status"] == "failed" and retry["attempts"] == 1
        assert db.mark_email_sending(batch["id"])
        db.finish_email_batch(batch["id"], "provider-123")
        assert not db.mark_email_sending(batch["id"])
        db.fail_email_batch(batch["id"], "Late failure cannot undo confirmed delivery")
        assert db.pending_email_batch(recipient) is None
        assert db.pending_notifications(recipient) == []
        assert db.counts()["emails_sent"] == 1
        assert [item["property_id"] for item in db.pending_notifications("other@example.com")] == [
            1,
            4,
        ]
        other = db.create_email_batch(
            "other@example.com", [ids[1], ids[4]], "Other", "html", "text"
        )
        assert db.mark_email_sending(other["id"])
        db.fail_email_batch(other["id"], "Connection lost after submission", indeterminate=True)
        assert db.pending_email_batch("other@example.com")["status"] == "unknown"
        assert db.pending_notifications("other@example.com") == []
        assert db.counts()["emails_unknown"] == 1
        interrupted = db.create_email_batch(
            "third@example.com", [ids[1], ids[4]], "Third", "html", "text"
        )
        assert db.mark_email_sending(interrupted["id"])
        assert db.pending_email_batch("third@example.com")["status"] == "unknown"
        assert db.counts()["emails_unknown"] == 2
        assert not db.mark_email_sending(interrupted["id"])
        assert not db.connection.execute("PRAGMA review.foreign_key_check").fetchall()


def test_email_migration_and_deletion_preserve_sent_property_history(tmp_path):
    path = tmp_path / "archive.sqlite"
    recipient = "me@example.com"
    with ReviewDatabase(path) as db:
        db.register_profile(PROFILE, "bright flat", "example-model")
        store_ready(db, listing())
        review_id, _ = db.claim_review(PROFILE, 1)
        assert db.complete_review(review_id, JUDGEMENT)
        original = dict(
            db.connection.execute(
                "SELECT * FROM review.property_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        )
        # Reconstruct the immediately previous sidecar shape without touching reviews.
        db.connection.executescript("""
            DROP TABLE review.email_batch_items;
            DROP TABLE review.email_batches;
            PRAGMA review.user_version = 2;
        """)
    with ReviewDatabase(path) as db:
        assert (
            dict(
                db.connection.execute(
                    "SELECT * FROM review.property_reviews WHERE id = ?",
                    (review_id,),
                ).fetchone()
            )
            == original
        )
        assert (
            db.connection.execute("PRAGMA review.user_version").fetchone()[0]
            == REVIEW_SCHEMA_VERSION
            == 4
        )
        batch = db.create_email_batch(
            recipient, [review_id], "Stable subject", "Saved HTML", "Saved text"
        )
        assert db.mark_email_sending(batch["id"])
        db.finish_email_batch(batch["id"], None)
        assert db.delete_properties([1]) == 1
        assert db.counts()["property_reviews"] == 0
        assert db.counts()["email_batch_items"] == 1
        assert db.counts()["emails_sent"] == 1
    for _ in range(2):
        with ReviewDatabase(path) as db:
            db.register_profile(OTHER_PROFILE, "different criteria", "example-model")
            store_ready(
                db, replace(listing(), title="Relisted at a new price", rent_pcm_pence=250000)
            )
            if (claimed := db.claim_review(OTHER_PROFILE, 1)) is not None:
                assert db.complete_review(claimed[0], JUDGEMENT)
            assert db.pending_notifications(recipient) == []
            assert [
                item["property_id"] for item in db.pending_notifications("new@example.com")
            ] == [1]
            assert db.connection.execute(
                "SELECT subject, html_body, text_body FROM review.email_batches WHERE id = ?",
                (batch["id"],),
            ).fetchone()[:] == ("Stable subject", "Saved HTML", "Saved text")
            assert not db.connection.execute("PRAGMA review.foreign_key_check").fetchall()


def test_v3_sender_migration_preserves_history_and_only_changes_safe_retries(tmp_path, monkeypatch):
    path = tmp_path / "sender-migration.sqlite"
    old_sender = "flats@spanashis.com"
    new_sender = "notifications@flats.spanashis.com"
    statuses = ("pending", "failed", "sent", "unknown", "sending")
    with ReviewDatabase(path) as db:
        db.register_profile(PROFILE, "bright flat", "example-model")
        store_ready(db, listing())
        review_id, _ = db.claim_review(PROFILE, 1)
        assert db.complete_review(review_id, JUDGEMENT)
        old_schema = (Path(__file__).parents[1] / "src/openrent/review_schema.sql").read_text()
        old_schema = old_schema.replace(
            "sender TEXT NOT NULL CHECK (sender <> '')",
            "sender TEXT NOT NULL DEFAULT 'flats@spanashis.com' "
            "CHECK (sender = 'flats@spanashis.com')",
        ).replace("PRAGMA review.user_version = 4;", "PRAGMA review.user_version = 3;")
        db.connection.executescript(
            "DROP TABLE review.email_batch_items; DROP TABLE review.email_batches;" + old_schema
        )
        with db.connection:
            for batch_id, status in enumerate(statuses, start=41):
                recipient = f"{status}@example.com"
                db.connection.execute(
                    "INSERT INTO review.email_batches "
                    "(id, recipient, sender, subject, html_body, text_body, status, "
                    "provider_message_id, error, created_at, last_attempt_at, sent_at, attempts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        recipient,
                        old_sender,
                        f"Saved subject {status}",
                        f"<p>Full frozen HTML {status}</p>",
                        f"Full frozen text {status}",
                        status,
                        "old-provider-id" if status == "sent" else None,
                        "Original failure" if status in {"failed", "unknown"} else None,
                        "old-created",
                        None if status == "pending" else "old-attempt",
                        "old-sent" if status == "sent" else None,
                        0 if status == "pending" else 2,
                    ),
                )
                db.connection.execute(
                    "INSERT INTO review.email_batch_items VALUES (?, ?, 1, ?)",
                    (batch_id, recipient, review_id),
                )
        original_batches = [
            dict(row)
            for row in db.connection.execute("SELECT * FROM review.email_batches ORDER BY id")
        ]
        original_items = [
            tuple(row)
            for row in db.connection.execute(
                "SELECT * FROM review.email_batch_items ORDER BY batch_id"
            )
        ]
        original_result = db.connection.execute(
            "SELECT json(result) FROM review.property_reviews WHERE id = ?", (review_id,)
        ).fetchone()[0]

    migrate = Database._migrate_review_notifications

    def interrupted(database, version):
        migrate(database, version)
        raise RuntimeError("Interrupted sender migration")

    monkeypatch.setattr(Database, "_migrate_review_notifications", interrupted)
    with pytest.raises(RuntimeError, match="Interrupted sender migration"):
        ReviewDatabase(path)
    with sqlite3.connect(review_path_for(path)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert [
            dict(row) for row in connection.execute("SELECT * FROM email_batches ORDER BY id")
        ] == original_batches
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    monkeypatch.setattr(Database, "_migrate_review_notifications", migrate)

    # Opening twice also proves the sender migration cannot rewrite later history.
    for _ in range(2):
        with ReviewDatabase(path) as db:
            assert db.connection.execute("PRAGMA review.user_version").fetchone()[0] == 4
            assert (
                next(
                    row["dflt_value"]
                    for row in db.connection.execute("PRAGMA review.table_info(email_batches)")
                    if row["name"] == "sender"
                )
                is None
            )
            expected = [
                dict(
                    row, sender=new_sender if row["status"] in {"pending", "failed"} else old_sender
                )
                for row in original_batches
            ]
            assert [
                dict(row)
                for row in db.connection.execute("SELECT * FROM review.email_batches ORDER BY id")
            ] == expected
            assert [
                tuple(row)
                for row in db.connection.execute(
                    "SELECT * FROM review.email_batch_items ORDER BY batch_id"
                )
            ] == original_items
            assert (
                db.connection.execute(
                    "SELECT json(result) FROM review.property_reviews WHERE id = ?", (review_id,)
                ).fetchone()[0]
                == original_result
            )
            assert db.pending_notifications("sent@example.com") == []
            failed = db.pending_email_batch("failed@example.com")
            assert failed["id"] == 42 and failed["sender"] == new_sender
            assert failed["html_body"] == original_batches[1]["html_body"]
            assert "email_batches_recipient_status" in {
                row["name"]
                for row in db.connection.execute("PRAGMA review.index_list(email_batches)")
            }
            assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
            assert not db.connection.execute("PRAGMA review.foreign_key_check").fetchall()
    with monkeypatch.context() as future_sender:
        future_sender.setattr("openrent.review_db.SENDER_EMAIL", "future@flats.spanashis.com")
        with ReviewDatabase(path) as db:
            for status in ("pending", "failed"):
                refreshed = db.pending_email_batch(f"{status}@example.com")
                original = next(row for row in original_batches if row["status"] == status)
                assert refreshed == dict(original, sender="future@flats.spanashis.com")
            assert db.pending_email_batch("unknown@example.com")["sender"] == old_sender
            assert {
                row[0]
                for row in db.connection.execute(
                    "SELECT sender FROM review.email_batches WHERE status IN ('sent', 'sending')"
                )
            } == {old_sender}
            assert db.connection.execute("PRAGMA review.user_version").fetchone()[0] == 4
    with ReviewDatabase(path) as db:
        created = db.create_email_batch("new@example.com", [review_id], "New", "HTML", "Text")
        assert created["id"] == 46 and created["sender"] == new_sender
        with pytest.raises(sqlite3.IntegrityError):
            db.create_email_batch("sent@example.com", [review_id], "Duplicate", "HTML", "Text")
