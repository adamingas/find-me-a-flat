"""Review configuration and saved decisions through the durable review workflow."""

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from archive_helpers import CONFIG, listing
from dbos import DBOS
from pipeline_worker import judgement

from openrent import db as database
from openrent import review
from openrent.ingestion import REVIEW_QUEUE
from openrent.review import configuration, review_property


def arguments(criteria, **overrides):
    return SimpleNamespace(
        **{
            "criteria_file": criteria,
            "review_model": "chosen-model",
            "review_timeout": 10,
            "review_backend": "responses",
            "review_api": "openai",
            "review_provider": None,
            "env_file": criteria.parent / "private.env",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "value, message",
    [
        ({"criteria": " "}, "between 1 and 64000"),
        ({"criteria": "a" * 64001}, "between 1 and 64000"),
        ({"review_timeout": 0}, "positive and finite"),
        ({"review_timeout": float("inf")}, "positive and finite"),
        ({"review_timeout": float("nan")}, "positive and finite"),
        ({"review_model": " "}, "Supply --review-model"),
        ({"review_backend": "codex", "review_api": "openrouter"}, "require --review-backend"),
        ({"review_provider": "provider"}, "requires --review-api openrouter"),
    ],
)
def test_invalid_review_settings_fail_before_starting_ai(tmp_path, monkeypatch, value, message):
    criteria = tmp_path / "criteria.txt"
    value = dict(value)
    criteria.write_text(value.pop("criteria", "Bright flats"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-key")
    with pytest.raises(ValueError, match=message):
        configuration(arguments(criteria, **value))


def test_review_config_does_not_checkpoint_credentials_and_resolves_them_at_execution(
    tmp_path, monkeypatch
):
    criteria = tmp_path / "criteria.txt"
    criteria.write_text("Bright flats")
    env = tmp_path / "private.env"
    env.write_text("OPENAI_API_KEY=private-file-token\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = configuration(arguments(criteria))
    assert config.model_configuration().api_key == "private-file-token"
    assert "private-file-token" not in repr(asdict(config))
    monkeypatch.setenv("OPENAI_API_KEY", "private-environment-token")
    assert config.model_configuration().api_key == "private-environment-token"
    assert configuration(arguments(criteria)).profile_key == config.profile_key
    criteria.write_text("Different preferences")
    assert configuration(arguments(criteria)).profile_key != config.profile_key


@pytest.mark.asyncio
async def test_reviews_save_decisions_and_skip_completed_properties(pipeline, monkeypatch):
    calls = []

    async def judge(evidence, criteria, **kwargs):
        assert criteria == CONFIG.criteria
        assert [image.content for image in evidence.images] == [b"original image"]
        calls.append(evidence.property_id)
        return judgement({1: True, 2: None, 3: False, 4: True}[evidence.property_id])

    monkeypatch.setattr(review, "judge_property", judge)
    for pid in range(1, 5):
        prop = listing(pid)
        await database.upsert_property(pipeline.db, prop)
        await database.store_image(
            pipeline.db, pid, prop.images[0], b"original image", "image/jpeg"
        )
    handles = [
        await DBOS.enqueue_workflow_async(REVIEW_QUEUE, review_property, pid, CONFIG)
        for pid in range(1, 5)
    ]
    async with asyncio.timeout(15):
        assert all(await asyncio.gather(*(handle.get_result() for handle in handles)))
    assert sorted(calls) == [1, 2, 3, 4]
    records = await database.pending_notifications(pipeline.db, "self@example.com")
    assert [(r["property_id"], r["result"]["decision"]) for r in records] == [
        (1, "pass"),
        (2, "uncertain"),
        (4, "pass"),
    ]
    # A newly requested run with another profile still respects once-per-property review.
    handle = await DBOS.start_workflow_async(
        review_property, 1, replace(CONFIG, criteria="Other criteria", profile_key="b" * 64)
    )
    assert await handle.get_result() is None
    assert len(calls) == 4
