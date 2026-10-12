"""Own shared resources and configure independent DBOS discovery/digest schedules."""

import asyncio
import signal
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter
from dbos import DBOS

from . import db as database
from .amenity_client import AmenityClient
from .client import OpenRentClient
from .ingestion import (
    DISCOVERY_QUEUE,
    NOTIFY_QUEUE,
    PROPERTY_QUEUE,
    REVIEW_QUEUE,
    DiscoveryConfig,
    discover,
)
from .locking import ScanLock
from .notifications import NotificationSettings, notify
from .notifications import configuration as notification_configuration
from .review import configuration as review_configuration
from .runtime import Runtime, configure


def system_path_for(database):
    return Path(str(database.resolve()) + ".dbos.sqlite")


async def register_queues(concurrency, review_concurrency):
    for name, workers in (
        (DISCOVERY_QUEUE, 1),
        (PROPERTY_QUEUE, concurrency),
        (REVIEW_QUEUE, review_concurrency),
        (NOTIFY_QUEUE, 1),
    ):
        await DBOS.register_queue_async(name, worker_concurrency=workers)


def schedule_inputs(args, discovery, notification):
    entries = [
        {
            "schedule_name": "discover",
            "workflow_fn": discover,
            "schedule": args.cron,
            "context": discovery,
            "cron_timezone": args.timezone,
            "queue_name": DISCOVERY_QUEUE,
        }
    ]
    if notification is not None:
        entries.append(
            {
                "schedule_name": "notify",
                "workflow_fn": notify,
                "schedule": args.notify_cron_schedule,
                "context": notification,
                "cron_timezone": args.timezone,
                "queue_name": NOTIFY_QUEUE,
            }
        )
    return entries


async def run_daemon(args, api, website):
    ZoneInfo(args.timezone)
    for expression in (args.cron, args.notify_cron_schedule):
        if expression is not None and (
            len(expression.split()) != 5 or not croniter.is_valid(expression)
        ):
            raise ValueError("Cron schedules must contain five valid fields.")
    configured_notification = notification_configuration(args)
    if configured_notification is not None and not args.notify_cron_schedule:
        raise ValueError("Email delivery requires --notify-cron-schedule.")
    if args.notify_cron_schedule and configured_notification is None:
        raise ValueError("--notify-cron-schedule requires --email-to.")
    discovery = DiscoveryConfig(
        api,
        website,
        args.post_filter,
        review_configuration(args) if args.criteria_file else None,
    )
    notification = None
    if configured_notification is not None:
        notification = NotificationSettings(
            args.db.resolve(),
            configured_notification.recipients,
            configured_notification.preview.resolve() if configured_notification.preview else None,
            args.email_timeout,
            Path(args.env_file or ".env").resolve(),
        )
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        with ScanLock(args.db):
            async with (
                OpenRentClient(
                    timeout=args.timeout,
                    concurrency=args.concurrency,
                    requests_per_second=args.requests_per_minute / 60,
                    cookie_file=args.cookie_file,
                ) as client,
                database.connect(args.db) as db,
                AmenityClient(timeout=max(60, args.timeout)) as amenity_client,
            ):
                configure(Runtime(client, db, args.quiet, amenity_client))
                dbos = None
                try:
                    dbos = DBOS(
                        config={
                            "name": "openrent",
                            "system_database_url": f"sqlite:///{system_path_for(args.db)}",
                            "log_level": "WARNING" if args.quiet else "INFO",
                        }
                    )
                    # Launch on this event loop so queued async work shares the client and limiter.
                    DBOS.launch()
                    await register_queues(args.concurrency, args.review_concurrency)
                    await DBOS.apply_schedules_async(schedule_inputs(args, discovery, notification))
                    if notification is None:
                        await DBOS.delete_schedule_async("notify")
                    if args.run_now:
                        await DBOS.enqueue_workflow_async(
                            DISCOVERY_QUEUE, discover, datetime.now(UTC), discovery
                        )
                    await stop.wait()
                finally:
                    # DBOS 3.2 adopts our main loop but destroy() only cancels its
                    # own background loop. Stop its pollers, then cancel and join
                    # main-loop work before destroy closes its executor/database.
                    if dbos is not None:
                        for event in dbos.background_thread_stop_events:
                            event.set()
                    tasks = [
                        task for task in asyncio.all_tasks() if task is not asyncio.current_task()
                    ]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await asyncio.to_thread(DBOS.destroy)
                    configure(None)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    return 0
