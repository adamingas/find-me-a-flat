"""Saved review evidence and delivery accounting survive archive updates."""

import json
import sqlite3
from dataclasses import replace

import pytest
from archive_helpers import CONFIG, assessment, listing, store_listing

from openrent import db as database
from openrent.models import Image


@pytest.mark.asyncio
async def test_evidence_requires_every_gallery_blob_and_a_property():
    async with database.connect(":memory:") as db:
        with pytest.raises(ValueError, match="missing from the archive"):
            await database.get_evidence(db, 1)
        prop = replace(
            listing(),
            images=[
                *listing().images,
                Image("https://images.openrent.co.uk/bedroom.jpg", position=1),
            ],
        )
        await database.upsert_property(db, prop)
        await database.store_image(db, 1, prop.images[0], b"living", "image/jpeg")
        with pytest.raises(ValueError, match="missing image content"):
            await database.get_evidence(db, 1)
        await database.store_image(db, 1, prop.images[1], b"bedroom", "image/jpeg")
        snapshot = await database.get_evidence(db, 1)
        assert [image.content for image in snapshot.images] == [b"living", b"bedroom"]
        assert snapshot.data["extra_metadata"] == prop.extra_metadata
        assert snapshot.data["features"][0]["value_integer"] == 2
        await database.query(db, "PRAGMA foreign_keys = OFF")
        await database.query(db, "DELETE FROM image_blobs WHERE content = ?", (b"living",))
        with pytest.raises(ValueError, match="missing image content"):
            await database.get_evidence(db, 1)


@pytest.mark.asyncio
async def test_saved_review_preserves_assessed_facts_and_original_gallery_bytes():
    async with database.connect(":memory:") as db:
        prop = replace(listing(), title=None, rent_pcm_pence=None)
        await store_listing(db, prop)
        original = await database.get_evidence(db, 1)
        record = assessment(original)
        review_id = await database.save_review(db, CONFIG, record)
        assert await database.save_review(db, CONFIG, record) == review_id
        await database.store_image(db, 1, prop.images[0], b"replacement bytes", "image/jpeg")
        await database.upsert_property(
            db,
            replace(
                prop,
                title="New title",
                rent_pcm_pence=250000,
                images=[Image("https://images.openrent.co.uk/new.jpg")],
            ),
        )
        assert await database.has_review(db, 1)
        reviewed = (await database.pending_notifications(db, "self@example.com"))[0]
        assert reviewed["title"] is None and reviewed["rent_pcm_pence"] is None
        assert reviewed["property"]["images"][0]["source_url"] == prop.images[0].source_url
        assert reviewed["result"] == json.loads(record.result)
        row = await database.fetch_one(
            db, "SELECT b.content FROM review_images i JOIN image_blobs b ON b.sha256=i.sha256"
        )
        assert row[0] == b"original image"


@pytest.mark.asyncio
async def test_failed_review_output_write_rolls_back_profile_result_and_gallery():
    async with database.connect(":memory:") as db:
        await store_listing(db, listing())
        record = assessment(await database.get_evidence(db, 1))
        invalid = replace(record, gallery=({**record.gallery[0], "content_sha256": "f" * 64},))
        with pytest.raises(sqlite3.IntegrityError):
            await database.save_review(db, CONFIG, invalid)
        for table in (
            "review_profiles",
            "property_reviews",
            "review_images",
            "notification_outbox",
        ):
            assert (await database.fetch_one(db, f"SELECT count(*) FROM {table}"))[0] == 0
        assert not await database.has_review(db, 1)
        assert await database.save_review(db, CONFIG, record) > 0


@pytest.mark.asyncio
async def test_review_and_notification_handoff_commit_atomically():
    async with database.connect(":memory:") as db:
        await store_listing(db, listing())
        record = assessment(await database.get_evidence(db, 1))
        await database.query(
            db,
            "CREATE TRIGGER fail_handoff BEFORE INSERT ON notification_outbox "
            "BEGIN SELECT RAISE(ABORT, 'outbox unavailable'); END;",
        )
        with pytest.raises(sqlite3.IntegrityError, match="outbox unavailable"):
            await database.save_review(db, CONFIG, record)
        assert not await database.has_review(db, 1)
        assert await database.pending_notifications(db, "self@example.com") == []
        await database.query(db, "DROP TRIGGER fail_handoff")
        review_id = await database.save_review(db, CONFIG, record)
        assert await database.has_review(db, 1)
        assert (await database.pending_notifications(db, "self@example.com"))[0][
            "review_id"
        ] == review_id
        assert await database.save_review(db, CONFIG, record) == review_id
        assert len(await database.pending_notifications(db, "self@example.com")) == 1


@pytest.mark.asyncio
async def test_completed_live_passes_and_uncertain_reviews_are_notifiable():
    async with database.connect(":memory:") as db:
        ids = {}
        for pid, decision, is_live in (
            (1, "pass", True),
            (2, "reject", True),
            (3, "pass", False),
            (4, "uncertain", True),
        ):
            await store_listing(db, replace(listing(pid), is_live=is_live))
            ids[pid] = await database.save_review(
                db, CONFIG, assessment(await database.get_evidence(db, pid), decision)
            )
        await store_listing(db, listing(5))
        assert [
            r["property_id"] for r in await database.pending_notifications(db, "self@example.com")
        ] == [1, 4]
        for review_id in (ids[2], ids[3], 999999):
            with pytest.raises(ValueError, match="ineligible"):
                await database.create_email_batch(
                    db, "self@example.com", [review_id], "Invalid", "html", "text"
                )
        assert (await database.fetch_one(db, "SELECT count(*) FROM email_batches"))[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_ids, subject, html, text, error",
    [
        ([], "Subject", "html", "text", ValueError),
        ([1, 1], "Subject", "html", "text", ValueError),
        ([1], "", "html", "text", ValueError),
        ([1], "Subject\r\nBcc: injected", "html", "text", ValueError),
        ([1], "Subject", None, "text", TypeError),
    ],
)
async def test_invalid_batches_do_not_reserve_properties(review_ids, subject, html, text, error):
    async with database.connect(":memory:") as db:
        await store_listing(db, listing())
        await database.save_review(db, CONFIG, assessment(await database.get_evidence(db, 1)))
        with pytest.raises(error):
            await database.create_email_batch(
                db, "self@example.com", review_ids, subject, html, text
            )
        assert len(await database.pending_notifications(db, "self@example.com")) == 1


@pytest.mark.asyncio
async def test_batches_reserve_once_per_property_and_recipient_and_retry_frozen_content():
    async with database.connect(":memory:") as db:
        await store_listing(db, listing())
        rid = await database.save_review(db, CONFIG, assessment(await database.get_evidence(db, 1)))
        batch = await database.create_email_batch(
            db, "PERSON@EXAMPLE.COM", [rid], "Original", "HTML", "Text"
        )
        assert batch["recipient"] == "person@example.com"
        with pytest.raises(sqlite3.IntegrityError):
            await database.create_email_batch(
                db, "person@example.com", [rid], "Duplicate", "html", "text"
            )
        assert (await database.fetch_one(db, "SELECT count(*) FROM email_batches"))[0] == 1
        assert await database.pending_notifications(db, "person@example.com") == []
        assert len(await database.pending_notifications(db, "other@example.com")) == 1
        assert await database.mark_email_sending(db, batch["id"])
        await database.fail_email_batch(db, batch["id"], "Definite refusal")
        retry = await database.pending_email_batch(db, "person@example.com")
        assert [
            retry[key] for key in ("subject", "html_body", "text_body", "attempts", "status")
        ] == ["Original", "HTML", "Text", 1, "failed"]
        assert await database.mark_email_sending(db, batch["id"])
        await database.finish_email_batch(db, batch["id"], "provider-123")
        assert not await database.mark_email_sending(db, batch["id"])
        await database.fail_email_batch(db, batch["id"], "Late failure")
        assert await database.pending_email_batch(db, "person@example.com") is None
        assert await database.pending_notifications(db, "person@example.com") == []
        await database.query(db, "DELETE FROM properties WHERE id=1")
        await store_listing(db, listing())
        await database.save_review(db, CONFIG, assessment(await database.get_evidence(db, 1)))
        assert await database.pending_notifications(db, "person@example.com") == []
        assert not await database.fetch_all(db, "PRAGMA foreign_key_check")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_failure", [True, False])
async def test_unknown_or_interrupted_email_attempt_is_reserved_without_retry(explicit_failure):
    async with database.connect(":memory:") as db:
        await store_listing(db, listing())
        rid = await database.save_review(db, CONFIG, assessment(await database.get_evidence(db, 1)))
        batch = await database.create_email_batch(
            db, "self@example.com", [rid], "Subject", "HTML", "Text"
        )
        assert await database.mark_email_sending(db, batch["id"])
        if explicit_failure:
            await database.fail_email_batch(db, batch["id"], "Lost response", indeterminate=True)
        assert (await database.pending_email_batch(db, "self@example.com"))["status"] == "unknown"
        assert not await database.mark_email_sending(db, batch["id"])
        assert await database.pending_notifications(db, "self@example.com") == []


@pytest.mark.asyncio
async def test_async_functions_store_outputs_and_delivery_history(tmp_path):
    async def run():
        async with database.connect(tmp_path / "archive.sqlite") as db:
            prop = listing()
            await database.upsert_property(db, prop)
            await database.store_image(db, 1, prop.images[0], b"original image", "image/jpeg")
            assert not await database.has_review(db, 1)
            rid = await database.save_review(
                db, CONFIG, assessment(await database.get_evidence(db, 1))
            )
            assert await database.has_review(db, 1)
            batch = await database.create_email_batch(
                db, "self@example.com", [rid], "Subject", "HTML", "Text"
            )
            assert (await database.pending_email_batch(db, "self@example.com"))["id"] == batch["id"]
            assert await database.mark_email_sending(db, batch["id"])
            await database.fail_email_batch(db, batch["id"], "Definite refusal")
            assert (await database.pending_email_batch(db, "self@example.com"))[
                "status"
            ] == "failed"
            assert await database.mark_email_sending(db, batch["id"])
            await database.finish_email_batch(db, batch["id"], None)
            assert await database.pending_email_batch(db, "self@example.com") is None
            assert await database.pending_notifications(db, "self@example.com") == []

    await run()
