"""Scheduled discovery and notification, with independent download/review consumers."""

import asyncio
import signal
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .async_db import AsyncDatabase
from .client import FetchError, OpenRentClient
from .download_jobs import DownloadJobs
from .ingestion import discover, drain, log
from .locking import ScanLock
from .notifications import configuration as notification_configuration
from .notifications import process_pending as notify
from .review import configuration, process_pending


async def run_daemon(args, api, website):
    try:
        timezone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {args.timezone}") from exc
    for expression in (args.cron, args.notify_cron_schedule):
        if expression is not None:
            if len(expression.split()) != 5:
                raise ValueError("Cron schedules must contain five fields.")
            croniter(expression, datetime.now(timezone))
    if (args.email_to or args.email_preview) and not args.notify_cron_schedule:
        raise ValueError("Email delivery requires --notify-cron-schedule.")
    notify_config = notification_configuration(args)
    if args.notify_cron_schedule and notify_config is None:
        raise ValueError("--notify-cron-schedule requires --email-to.")
    review_config = configuration(args) if args.criteria_file else None

    async def attempt(operation):
        try:
            await operation()
        except (FetchError, ValueError, sqlite3.Error, OSError) as exc:
            log(args, f"{exc}; work remains queued for retry.")

    async def consume(operation):
        while True:
            await attempt(operation)
            await asyncio.sleep(5)

    async def scheduled(operation, expression, *, run_now=False):
        if run_now:
            await attempt(operation)
        while True:
            next_tick = croniter(expression, datetime.now(timezone)).get_next(datetime)
            await asyncio.sleep(max(0, next_tick.timestamp() - datetime.now(timezone).timestamp()))
            await attempt(operation)

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        with ScanLock(args.db):
            async with (
                OpenRentClient(
                    timeout=args.timeout,
                    concurrency=args.concurrency,
                    requests_per_second=args.requests_per_minute / 60,
                    cookie_file=args.cookie_file,
                ) as client,
                AsyncDatabase(args.db) as db,
                DownloadJobs(args.db) as jobs,
                asyncio.TaskGroup() as tasks,
            ):
                tasks.create_task(
                    scheduled(
                        lambda: discover(args, client, jobs, api, website),
                        args.cron,
                        run_now=args.run_now,
                    ),
                    name="discovery",
                )
                tasks.create_task(consume(lambda: drain(args, client, db, jobs)), name="download")
                if review_config:
                    tasks.create_task(
                        consume(lambda: process_pending(review_config, quiet=args.quiet)),
                        name="review",
                    )
                if notify_config:
                    tasks.create_task(
                        scheduled(
                            lambda: notify(args.db, notify_config, quiet=args.quiet),
                            args.notify_cron_schedule,
                        ),
                        name="notify",
                    )
    except asyncio.CancelledError:
        log(args, "Service stopped; unfinished work remains queued.")
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
    return 0
