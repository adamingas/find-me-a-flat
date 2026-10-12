"""Durable AI assessment followed by an idempotent archive write."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbos import DBOS
from dotenv import dotenv_values

from . import db as database
from .judge import judge_property
from .model_config import api_configuration


@dataclass(frozen=True)
class ReviewConfig:
    criteria: str
    profile_key: str
    model: str
    timeout: float
    backend: str = "codex"
    api: str = "openai"
    provider: str | None = None
    env_file: Path = Path(".env")

    def model_configuration(self):
        if self.backend != "responses":
            return None
        settings = dict(dotenv_values(self.env_file)) if self.env_file.is_file() else {}
        return api_configuration(self.model, self.api, self.provider, settings)


def configuration(args) -> ReviewConfig:
    criteria = args.criteria_file.read_text(encoding="utf-8").strip()
    if not criteria or len(criteria) > 64000:
        raise ValueError("The criteria file must contain between 1 and 64000 characters.")
    if not math.isfinite(args.review_timeout) or args.review_timeout <= 0:
        raise ValueError("--review-timeout must be positive and finite.")
    model = args.review_model.strip() if args.review_model else None
    if not model:
        raise ValueError("Supply --review-model or set OPENRENT_REVIEW_MODEL.")
    backend, api, provider = args.review_backend, args.review_api, args.review_provider
    if backend != "responses" and (api != "openai" or provider is not None):
        raise ValueError("--review-api and --review-provider require --review-backend responses.")
    if api != "openrouter" and provider is not None:
        raise ValueError("--review-provider requires --review-api openrouter.")
    if backend == "codex" and shutil.which("codex") is None:
        raise ValueError("Install the Codex CLI and authenticate it before reviewing.")
    provider = provider.strip().casefold() if provider is not None else None
    identity = json.dumps([backend, criteria, model, api, provider])
    config = ReviewConfig(
        criteria,
        hashlib.sha256(identity.encode()).hexdigest(),
        model,
        args.review_timeout,
        backend,
        api,
        provider,
        Path(args.env_file or ".env").resolve(),
    )
    config.model_configuration()  # Validate credentials without storing them in DBOS.
    return config


@dataclass(frozen=True)
class Assessment:
    property_id: int
    fingerprint: str
    facts: dict[str, Any]
    gallery: tuple[dict[str, Any], ...]
    result: str


@DBOS.step()
async def needs_review(property_id: int) -> bool:
    from .runtime import resources

    return not await database.has_review(resources().db, property_id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def assess_property(property_id: int, config: ReviewConfig) -> Assessment:
    from .runtime import resources

    runtime = resources()
    evidence = await database.get_evidence(runtime.db, property_id)
    model_config = config.model_configuration()
    if model_config is not None:
        await model_config.validate(images=True, structured_output=True)
    runtime.report(f"Reviewing {property_id}.")
    judgement = await judge_property(
        evidence,
        config.criteria,
        model=config.model,
        backend=config.backend,
        timeout=config.timeout,
        model_config=model_config,
    )
    # Checkpoint the model result and digest facts, never another copy of image bytes.
    return Assessment(
        property_id,
        evidence.fingerprint,
        {
            name: evidence.data[name]
            for name in (
                "title",
                "url",
                "address_display",
                "rent_pcm_pence",
                "bedrooms",
                "currency",
                "postcode",
                "latitude",
                "longitude",
                "nearby_places",
                "images",
                "is_live",
            )
        },
        tuple(evidence.data["images"]),
        judgement.model_dump_json(),
    )


@DBOS.step()
async def save_assessment(config: ReviewConfig, assessment: Assessment) -> int:
    from .runtime import resources

    runtime = resources()
    review_id = await database.save_review(runtime.db, config, assessment)
    result = json.loads(assessment.result)
    runtime.report(f"{assessment.property_id}: {result['decision']} — {result['summary']}")
    return review_id


@DBOS.workflow()
async def review_property(property_id: int, config: ReviewConfig) -> int | None:
    if not await needs_review(property_id):
        return None
    assessment = await assess_property(property_id, config)
    return await save_assessment(config, assessment)
