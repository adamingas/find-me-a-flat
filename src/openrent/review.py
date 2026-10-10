"""Review each new listing once and store its suitability decision."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .judge import JudgeError, judge_property
from .locking import ScanLock
from .model_config import ModelConfig, api_configuration
from .review_db import AsyncReviewDatabase


@dataclass(frozen=True)
class ReviewConfig:
    database: Path
    criteria: str
    profile_key: str
    model: str
    timeout: float
    concurrency: int
    limit: int | None
    backend: str = "codex"
    model_config: ModelConfig | None = None


def configuration(args) -> ReviewConfig:
    criteria = args.criteria_file.read_text(encoding="utf-8").strip()
    if not criteria or len(criteria) > 64000:
        raise ValueError("The criteria file must contain between 1 and 64000 characters.")
    if not math.isfinite(args.review_timeout) or args.review_timeout <= 0:
        raise ValueError("--review-timeout must be positive and finite.")
    if args.review_limit is not None and args.review_limit <= 0:
        raise ValueError("--review-limit must be positive.")
    if args.review_concurrency < 1:
        raise ValueError("--review-concurrency must be positive.")
    model = args.review_model.strip() if args.review_model else None
    backend = args.review_backend
    api = getattr(args, "review_api", "openai")
    provider = getattr(args, "review_provider", None)
    model_config = None
    if backend != "responses" and (api != "openai" or provider is not None):
        raise ValueError("--review-api and --review-provider require --review-backend responses.")
    if api != "openrouter" and provider is not None:
        raise ValueError("--review-provider requires --review-api openrouter.")
    if not model:
        raise ValueError("Supply --review-model or set OPENRENT_REVIEW_MODEL.")
    if backend == "responses":
        env_file = Path(getattr(args, "env_file", None) or ".env")
        if env_file.exists() and not env_file.is_file():
            raise ValueError("The environment file must be a regular file.")
        settings = dict(dotenv_values(env_file)) if env_file.is_file() else {}
        model_config = api_configuration(model, api, provider, settings)
    if backend == "codex" and shutil.which("codex") is None:
        raise ValueError("Install the Codex CLI and authenticate it before reviewing.")
    # Criteria/model identify the audit trail, while completed property IDs are
    # excluded globally even when a later run supplies different conditions.
    identity = "openrent-agents-v5\0" + backend + "\0" + criteria + "\0" + model
    if api == "openrouter":
        identity += "\0" + json.dumps(
            model_config.audit_identity(),
            sort_keys=True,
        )
    key = hashlib.sha256(identity.encode()).hexdigest()
    return ReviewConfig(
        database=args.db,
        criteria=criteria,
        profile_key=key,
        model=model,
        timeout=args.review_timeout,
        concurrency=args.review_concurrency,
        limit=args.review_limit,
        backend=backend,
        model_config=model_config,
    )


async def process_pending(config: ReviewConfig, *, quiet=False) -> int:
    report = (lambda _: None) if quiet else print
    with ScanLock(Path(str(config.database.resolve()) + ".review")):
        async with AsyncReviewDatabase(config.database) as db:
            ids = await db.ready_ids(config.limit)
            if not ids:
                return 0
            if config.model_config is not None:
                await config.model_config.validate(images=True, structured_output=True)
            await db.register_profile(
                config.profile_key, config.criteria, config.model, config.backend
            )
            slots = asyncio.Semaphore(config.concurrency)
            failures = 0

            async def assess(property_id):
                nonlocal failures
                async with slots:
                    claimed = await db.claim_review(config.profile_key, property_id)
                    if claimed is None:
                        return
                    review_id, evidence = claimed
                    try:
                        report(f"Reviewing {property_id}.")
                        judgement = await judge_property(
                            evidence,
                            config.criteria,
                            model=config.model,
                            backend=config.backend,
                            timeout=config.timeout,
                            model_config=config.model_config,
                        )
                        await db.complete_review(review_id, judgement)
                        report(f"{property_id}: {judgement.decision} — {judgement.summary}")
                    except asyncio.CancelledError:
                        await db.fail_review(review_id, "Review interrupted; queued for retry.")
                        raise
                    except (JudgeError, OSError, ValueError) as exc:
                        await db.fail_review(review_id, str(exc))
                        failures += 1
                        report(f"{property_id}: review failed: {exc}")

            async with asyncio.TaskGroup() as group:
                for property_id in ids:
                    group.create_task(assess(property_id))
            return int(bool(failures))
