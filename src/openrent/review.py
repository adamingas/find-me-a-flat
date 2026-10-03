"""Review each new listing once and store its suitability decision."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .client import OpenRentClient
from .judge import JudgeError, judge_property
from .locking import ScanLock
from .notifications import NotificationConfig, send_pending
from .notifications import configuration as notification_configuration
from .review_db import AsyncReviewDatabase
from .review_images import prepare_gallery


@dataclass(frozen=True)
class ReviewConfig:
    database: Path
    criteria: str
    profile_key: str
    model: str | None
    timeout: float
    concurrency: int
    limit: int | None
    backend: str = "codex"
    stop_after_pass: bool = False
    notifications: NotificationConfig | None = None


def configuration(args) -> ReviewConfig:
    criteria = args.criteria_file.read_text(encoding="utf-8").strip()
    if not criteria or len(criteria) > 64000:
        raise ValueError("The criteria file must contain between 1 and 64000 characters.")
    if not math.isfinite(args.review_timeout) or args.review_timeout <= 0:
        raise ValueError("--review-timeout must be positive and finite.")
    if args.review_limit is not None and args.review_limit <= 0:
        raise ValueError("--review-limit must be positive.")
    if not 1 <= args.review_concurrency <= 4:
        raise ValueError("--review-concurrency must be between 1 and 4.")
    stop_after_pass = getattr(args, "stop_after_pass", False)
    if stop_after_pass and args.review_concurrency != 1:
        raise ValueError("--stop-after-pass requires --review-concurrency 1.")
    notifications = notification_configuration(args)
    if stop_after_pass and notifications is not None:
        raise ValueError("Email cycles must finish all pending reviews; omit --stop-after-pass.")
    model = args.review_model.strip() if args.review_model else None
    backend = args.review_backend
    if not args.dry_run:
        if not model:
            raise ValueError("Supply --review-model or set OPENRENT_REVIEW_MODEL.")
        if backend == "responses" and not os.environ.get("OPENAI_API_KEY", "").strip():
            raise ValueError("Set OPENAI_API_KEY for native Agents SDK requests.")
        if backend == "codex" and shutil.which("codex") is None:
            raise ValueError("Install the Codex CLI and authenticate it before reviewing.")
    # Criteria/model identify the audit trail, while completed property IDs are
    # excluded globally even when a later run supplies different conditions.
    key = hashlib.sha256(
        ("openrent-agents-v5\0" + backend + "\0" + criteria + "\0" + (model or "preview")).encode()
    ).hexdigest()
    return ReviewConfig(
        database=args.db,
        criteria=criteria,
        profile_key=key,
        model=model,
        timeout=args.review_timeout,
        concurrency=args.review_concurrency,
        limit=args.review_limit,
        backend=backend,
        stop_after_pass=stop_after_pass,
        notifications=notifications,
    )


async def process_pending(config: ReviewConfig, *, quiet=False, dry_run=False) -> int:
    if config.stop_after_pass and config.concurrency != 1:
        raise ValueError("--stop-after-pass requires --review-concurrency 1.")
    if config.stop_after_pass and config.notifications is not None:
        raise ValueError("Email cycles must finish all pending reviews; omit --stop-after-pass.")
    if not config.database.is_file():
        raise ValueError(f"Database does not exist: {config.database}; fetch listings first.")

    def report(message):
        if not quiet:
            print(message, flush=True)

    # Reviews can coexist with a scanner. Fingerprints are rechecked before a
    # decision is committed; a changed input remains unprocessed for retry.
    with ScanLock(Path(str(config.database.resolve()) + ".review")):
        async with AsyncReviewDatabase(config.database) as db:
            ids = await db.unprocessed_ids(limit=config.limit)
            if dry_run:
                print(
                    f"{len(ids)} unprocessed listings ready for review "
                    "(live; missing gallery images will be downloaded).",
                    flush=True,
                )
                for property_id in ids:
                    print(f"https://www.openrent.co.uk/{property_id}", flush=True)
                return 0
            if not ids:
                report("No new unprocessed listings; review cycle finished without an email.")
                return 0
            await db.register_profile(
                config.profile_key, config.criteria, config.model or "preview", config.backend
            )
            totals = {"pass": 0, "reject": 0, "uncertain": 0, "error": 0}
            attempted = set()
            review_slots = asyncio.Semaphore(config.concurrency)
            stopped = asyncio.Event()

            async def assess(property_id):
                async with review_slots:
                    if stopped.is_set():
                        return
                    claimed = await db.claim_review(
                        config.profile_key,
                        property_id,
                        once_per_property=True,
                    )
                    if claimed is None:
                        if property_id in await db.unprocessed_ids():
                            totals["error"] += 1
                            report(f"{property_id}: review evidence unavailable; left unprocessed.")
                        return
                    review_id, snapshot = claimed
                    try:
                        report(f"Reviewing {property_id} with {len(snapshot.images)} images.")
                        judgement = await judge_property(
                            snapshot.data,
                            snapshot.images,
                            config.criteria,
                            model=config.model,
                            backend=config.backend,
                            timeout=config.timeout,
                        )
                        if await db.complete_review(review_id, judgement):
                            totals[judgement.decision] += 1
                            report(f"{property_id}: {judgement.decision} — {judgement.summary}")
                            if config.stop_after_pass and judgement.decision == "pass":
                                stopped.set()
                        else:
                            totals["error"] += 1
                            report(
                                f"{property_id}: listing changed during review; left unprocessed."
                            )
                    except asyncio.CancelledError:
                        await db.fail_review(
                            review_id, "Review interrupted; retry on the next run."
                        )
                        raise
                    except (JudgeError, OSError, ValueError) as exc:
                        await db.fail_review(review_id, str(exc))
                        totals["error"] += 1
                        report(f"{property_id}: review failed; left unprocessed: {exc}")

            async with OpenRentClient() as client:

                async def prepare_and_assess(property_id):
                    if not await prepare_gallery(db, property_id, client=client, report=report):
                        totals["error"] += 1
                        return
                    await assess(property_id)

                while ids:
                    attempted.update(ids)
                    if config.stop_after_pass:
                        for property_id in ids:
                            await prepare_and_assess(property_id)
                            if stopped.is_set():
                                break
                    else:
                        tasks = [asyncio.create_task(prepare_and_assess(item)) for item in ids]
                        try:
                            await asyncio.gather(*tasks)
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                    # A scanner may add properties while the current jobs run.
                    # Drain fresh IDs, but leave failed attempts for the next run.
                    if config.limit is not None or stopped.is_set():
                        break
                    ids = [item for item in await db.unprocessed_ids() if item not in attempted]
            print(
                f"Reviews: {totals['pass']} pass, {totals['reject']} reject, "
                f"{totals['uncertain']} uncertain, {totals['error']} failed.",
                flush=True,
            )
            notification_result = 0
            if config.notifications is not None:
                pending_ids = await db.unprocessed_ids()
                remaining = (
                    [item for item in pending_ids if item in attempted]
                    if config.limit is not None
                    else pending_ids
                )
                if totals["error"] or remaining:
                    report(
                        f"Review cycle incomplete: {len(remaining)} listings remain unprocessed; "
                        "no email sent."
                    )
                    return 1
                notification_result = await send_pending(
                    db,
                    config.notifications,
                    report,
                    property_ids=attempted if config.limit is not None else None,
                )
            return 1 if totals["error"] or notification_result else 0


async def review_command(args) -> int:
    from .daemon import run_daemon, validate_schedule

    if args.check_schedule and not args.cron:
        raise ValueError("--check-schedule requires --cron.")
    if args.cron:
        validate_schedule(args.cron, args.timezone)
        if args.check_schedule:
            return await run_daemon(args, lambda _: asyncio.sleep(0, result=0))
    elif args.run_now or args.max_runs is not None:
        raise ValueError("--run-now and --max-runs require --cron.")
    config = configuration(args)

    async def run(_):
        return await process_pending(config, quiet=args.quiet, dry_run=args.dry_run)

    if args.cron and not args.dry_run:
        return await run_daemon(args, run)
    return await run(args)
