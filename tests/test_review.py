"""Exercise the complete review queue against a real SQLite archive."""

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from click.testing import CliRunner

from openrent import cli, review
from openrent.db import Database
from openrent.judge import JudgeError, Judgement
from openrent.models import Image, Property
from openrent.review_db import ReviewDatabase
from openrent.review_models import CriterionResult


def add_listing(path, property_id, **changes):
    pictures = [
        Image(f"https://images.openrent.co.uk/{property_id}/{number}.png", number)
        for number in range(2)
    ]
    values = {
        "id": property_id,
        "url": f"https://www.openrent.co.uk/{property_id}",
        "title": f"Flat {property_id}",
        "description": "Archived description",
        "rent_pcm_pence": 180_000,
        "is_live": True,
        "detail_complete": True,
        "images": pictures,
    }
    values.update(changes)
    with Database(path) as db:
        prop = Property(**values)
        db.upsert_property(prop)
        for picture in pictures:
            db.store_image(property_id, picture, f"photo-{property_id}".encode(), "image/png")


def config(path):
    return review.ReviewConfig(
        database=path,
        criteria="My generated test conditions",
        profile_key=hashlib.sha256(b"test-conditions").hexdigest(),
        model="test-model",
        timeout=10,
        concurrency=2,
        limit=None,
    )


def judgement(decision):
    outcome = {"pass": True, "reject": False, "uncertain": None}[decision]
    return Judgement.model_validate(
        {
            name: {"outcome": outcome, "evidence": "Image 1"}
            for name, field in Judgement.model_fields.items()
            if isinstance(field.annotation, type) and issubclass(field.annotation, CriterionResult)
        }
        | {
            "summary": f"Decision {decision}",
            "area_m2": None,
            "floor": {"value": 0.0, "evidence": "Listing states ground floor."},
        }
    )


def test_pipeline_retries_failures_reviews_each_id_once_and_bounds_concurrency(
    tmp_path, monkeypatch
):
    path = tmp_path / "archive.sqlite"
    for property_id in range(1, 5):
        add_listing(path, property_id)
    cfg = config(path)
    calls = []
    fail = True
    running = maximum = 0
    expected_backend = cfg.backend
    assert expected_backend == "codex"

    async def judge(snapshot, images, criteria, **kwargs):
        nonlocal running, maximum
        property_id = snapshot["id"]
        calls.append(property_id)
        assert criteria == cfg.criteria
        assert len(images) == 2
        assert all(item.content == f"photo-{property_id}".encode() for item in images)
        assert all(item.content_type == "image/png" for item in images)
        assert kwargs == {
            "model": cfg.model,
            "backend": expected_backend,
            "timeout": cfg.timeout,
        }
        running += 1
        maximum = max(maximum, running)
        try:
            await asyncio.sleep(0.01)
            if property_id == 4 and fail:
                raise JudgeError("temporary review failure")
            return judgement({1: "pass", 2: "reject", 3: "uncertain"}.get(property_id, "pass"))
        finally:
            running -= 1

    monkeypatch.setattr(review, "judge_property", judge)
    assert asyncio.run(review.process_pending(cfg, quiet=True)) == 1
    assert maximum == 2
    assert sorted(calls) == [1, 2, 3, 4]
    add_listing(path, 1, rent_pcm_pence=210_000, description="Updated after processing")
    fail = False
    assert asyncio.run(review.process_pending(cfg, quiet=True)) == 0
    assert calls.count(1) == calls.count(2) == calls.count(3) == 1
    assert calls.count(4) == 2
    assert asyncio.run(review.process_pending(cfg, quiet=True)) == 0
    with ReviewDatabase(path) as db:
        assert db.counts()["reviews_complete"] == 4
        assert [
            tuple(row)
            for row in db.connection.execute(
                "SELECT property_id, decision FROM review.property_reviews WHERE status = 'complete' "
                "ORDER BY property_id"
            )
        ] == [(1, "pass"), (2, "reject"), (3, "uncertain"), (4, "pass")]
        stored = json.loads(
            db.connection.execute(
                "SELECT json(result) FROM review.property_reviews WHERE property_id = 1"
            ).fetchone()[0]
        )
        assert stored == judgement("pass").result
        assert stored["no_living_room_carpet"]["breaking"] is True
        assert stored["bathroom_without_window"]["breaking"] is False
        assert stored["no_living_room_carpet"]["outcome"] is True
        assert "images_examined" not in stored
        assert stored["area_m2"] is None
        assert stored["floor"] == {"value": 0.0, "evidence": "Listing states ground floor."}
        assert "findings" not in stored
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
    other = replace(cfg, criteria="Different conditions", profile_key="f" * 64)
    assert asyncio.run(review.process_pending(other, quiet=True)) == 0
    assert len(calls) == 5
    criteria = tmp_path / "conditions.txt"
    criteria.write_text(cfg.criteria, encoding="utf-8")
    add_listing(path, 5)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENRENT_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("OPENRENT_REVIEW_BACKEND", raising=False)
    base = [
        "review",
        "--db",
        str(path),
        "--criteria-file",
        str(criteria),
        "--review-timeout",
        "10",
        "--quiet",
    ]
    responses = [*base, "--review-backend", "responses"]
    runner = CliRunner()
    result = runner.invoke(cli.app, [*responses, "--review-model", "test-model"])
    assert result.exit_code == 1 and "OPENAI_API_KEY" in result.output
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    result = runner.invoke(cli.app, responses)
    assert result.exit_code == 1 and "--review-model" in result.output
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(review.shutil, "which", lambda _: None)
    for backend in ("codex", "responses"):
        result = runner.invoke(cli.app, [*base, "--review-backend", backend, "--dry-run"])
        assert result.exit_code == 0 and "1 unprocessed" in result.output
    with ReviewDatabase(path) as db:
        assert (
            db.connection.execute("SELECT count(*) FROM review.property_reviews").fetchone()[0] == 4
        )
    assert len(calls) == 5

    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    expected_backend = "responses"
    result = runner.invoke(cli.app, [*responses, "--review-model", "test-model"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 6 and calls[-1] == 5
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(review.shutil, "which", lambda _: "/mock/bin/codex")
    result = runner.invoke(cli.app, [*base, "--review-model", "test-model"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 6


def test_daemon_reviews_independently_and_rejects_missing_images_before_discovery(
    tmp_path, monkeypatch
):
    criteria = tmp_path / "conditions.txt"
    criteria.write_text("Example test conditions", encoding="utf-8")
    events = []
    scan_result = 0

    async def discover(args, *_, **kwargs):
        events.append("discover")
        if scan_result:
            raise ValueError("Simulated discovery failure")
        return {}

    async def drain(*_):
        return 0

    async def process(cfg, **kwargs):
        events.append("review")
        assert cfg.backend == "responses" and cfg.model == "test-model"
        return 0

    monkeypatch.setattr(cli, "discover", discover)
    monkeypatch.setattr(cli, "drain", drain)
    monkeypatch.setattr(review, "process_pending", process)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    flags = [
        "daemon",
        "--location",
        "Victoria, London",
        "--radius-distance",
        "2",
        "--db",
        str(tmp_path / "archive.sqlite"),
        "--cron",
        "* * * * *",
        "--run-now",
        "--max-runs",
        "1",
        "--criteria-file",
        str(criteria),
        "--review-model",
        "test-model",
        "--review-backend",
        "responses",
    ]
    runner = CliRunner()
    result = runner.invoke(cli.app, flags)
    assert result.exit_code == 0, result.output
    assert events[0] == "discover" and "review" in events
    events.clear()
    scan_result = 1
    result = runner.invoke(cli.app, flags)
    assert result.exit_code == 1 and events[0] == "discover" and "review" in events
    events.clear()
    result = runner.invoke(cli.app, [*flags, "--skip-images"])
    assert result.exit_code == 1 and events == []
    assert "needs downloaded images" in result.output


def test_pipeline_cancellation_reaches_judge_and_leaves_listing_retryable(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite"
    add_listing(path, 1)
    cfg = replace(config(path), concurrency=1)
    calls = []

    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def judge(snapshot, images, criteria, **kwargs):
            calls.append(snapshot["id"])
            assert len(images) == 2 and all(item.content == b"photo-1" for item in images)
            assert kwargs == {"model": cfg.model, "backend": cfg.backend, "timeout": cfg.timeout}
            if len(calls) > 1:
                return judgement("pass")
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(review, "judge_property", judge)
        task = asyncio.create_task(review.process_pending(cfg, quiet=True))
        try:
            async with asyncio.timeout(3):
                await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert cancelled.is_set()
        with ReviewDatabase(path) as db:
            row = db.connection.execute(
                "SELECT status, processed_at, decision FROM review.property_reviews WHERE property_id = 1"
            ).fetchone()
            assert tuple(row) == ("error", None, None)
        assert await review.process_pending(cfg, quiet=True) == 0

    asyncio.run(scenario())
    assert calls == [1, 1]
    with ReviewDatabase(path) as db:
        row = db.connection.execute(
            "SELECT status, processed_at, decision FROM review.property_reviews WHERE property_id = 1"
        ).fetchone()
        assert row[0] == "complete" and row[1] is not None and row[2] == "pass"


@pytest.mark.parametrize("gallery", ["empty", "partial"])
def test_review_skips_incomplete_galleries_until_downloaded_without_using_its_limit(
    tmp_path, monkeypatch, gallery
):
    path = tmp_path / "archive.sqlite"
    add_listing(path, 1)
    add_listing(path, 2)
    with Database(path) as db, db.connection:
        if gallery == "empty":
            db.connection.execute("DELETE FROM property_images WHERE property_id = 1")
        else:
            db.connection.execute(
                "UPDATE property_images SET download_status = 'pending', content_sha256 = NULL "
                "WHERE property_id = 1 AND position = 1"
            )
    calls = []

    async def judge(snapshot, images, criteria, **kwargs):
        calls.append(snapshot["id"])
        assert len(images) == 2
        return judgement("pass")

    monkeypatch.setattr(review, "judge_property", judge)
    cfg = replace(config(path), limit=1)
    assert asyncio.run(review.process_pending(cfg, quiet=True)) == 0
    assert calls == [2]  # Incomplete ID 1 neither downloads nor consumes the limit.
    with ReviewDatabase(path) as db:
        assert db.counts()["reviews_complete"] == 1
        assert db.claim_review(cfg.profile_key, 1) is None
    add_listing(path, 1)  # The downloader finishes the gallery before the next cycle.
    assert asyncio.run(review.process_pending(cfg, quiet=True)) == 0
    assert calls == [2, 1]
    with ReviewDatabase(path) as db:
        assert db.counts()["reviews_complete"] == 2
        assert db.unprocessed_ids() == []
