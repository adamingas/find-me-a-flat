"""uv-run command line interface and resilient, repeatable import orchestration."""

import asyncio
import sqlite3
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import click

from .async_db import AsyncDatabase
from .client import FetchError, OpenRentClient
from .download_jobs import DownloadJobs
from .ingestion import discover, drain
from .search import ApiFilters, SearchError, WebsiteFilters


def money(value):
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
        return amount
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("Use a non-negative amount with at most 2 decimals.") from exc


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Date must be YYYY-MM-DD.") from exc


def scan_flags(command):
    """Apply the same Click options to manual and scheduled scans."""
    options = [
        click.option(
            "--location",
            required=True,
            help="Area, address, landmark, or postcode resolved by OpenRent (e.g. Victoria, London).",
        ),
        click.option(
            "--radius-distance", type=float, help="Radius in km (or --distance-unit miles)."
        ),
        click.option("--radius-minutes", type=int, help="OpenRent commute radius; London only."),
        click.option("--distance-unit", type=click.Choice(["km", "miles"]), default="km"),
        click.option("--rent-min", "--min-rent", type=money, help="Minimum monthly rent in GBP."),
        click.option("--rent-max", "--max-rent", type=money, help="Maximum monthly rent in GBP."),
        click.option("--bedrooms-min", "--min-bedrooms", type=int, help="0 includes studios."),
        click.option("--bedrooms-max", "--max-bedrooms", type=int),
        click.option("--bathrooms-min", type=int),
        click.option("--bathrooms-max", type=int),
        click.option(
            "--property-type",
            "property_types",
            multiple=True,
            type=click.Choice(["house", "flat", "room"]),
            help="Repeat to include multiple types.",
        ),
        click.option(
            "--furnishing", type=click.Choice(["any", "furnished", "unfurnished"]), default=None
        ),
    ]
    for name, help_text in (
        ("pets", "Pets allowed."),
        ("students", "Students accepted."),
        ("professionals", "Non-students accepted."),
        ("families", "Families accepted."),
        ("dss", "OpenRent's DSS/LHA covers rent or preferred indicator."),
        ("bills-included", "Bills included."),
        ("garden", "Garden access."),
        ("parking", "Parking available."),
        ("fireplace", "Fireplace available."),
        ("video", "Video tour or video viewings accepted."),
        ("no-shared", "Exclude rooms in shared homes."),
        ("no-studios", "Exclude studio flats."),
        ("today", "Only listings first listed today (Europe/London)."),
        ("include-unavailable", "Also import unavailable listings returned by the search."),
    ):
        options.append(
            click.option(
                "--" + name,
                is_flag=True,
                default=False if name == "include-unavailable" else None,
                help=help_text,
            )
        )
    options.extend(
        [
            click.option("--move-in-before", type=iso_date),
            click.option(
                "--max-minimum-tenancy",
                type=int,
                metavar="MONTHS",
                help="Accept listings requiring at most this many months.",
            ),
            click.option(
                "--sort",
                type=click.Choice(["distance", "rent-asc", "rent-desc", "newest"]),
                default="distance",
            ),
            click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite")),
            click.option("--skip-images", is_flag=True, help="Save image metadata without bytes."),
            click.option("--refresh", is_flag=True, help="Fetch saved listing metadata again."),
            click.option(
                "--filter/--no-filter",
                "post_filter",
                default=False,
                help="Delete imported listings unless Tube walk is at most 11 minutes and EPC is A/B/C. Default: off.",
            ),
            click.option(
                "--no-source-html", is_flag=True, help="Omit the sanitized listing HTML backup."
            ),
            click.option("--dry-run", is_flag=True, help="Show matches without creating a DB."),
            click.option(
                "--concurrency",
                type=click.IntRange(1, 32),
                default=1,
                show_default=True,
                help="Maximum simultaneous HTTP requests.",
            ),
            click.option("--timeout", type=float, default=30),
            click.option(
                "--requests-per-second",
                "--rps",
                type=click.FloatRange(min=0, min_open=True),
                default=0.2,
                show_default=True,
                help="Maximum request start rate across pages, images, redirects and retries.",
            ),
            click.option(
                "--cookie-file",
                type=click.Path(path_type=Path),
                help="Optional Netscape cookies exported from your own OpenRent session.",
            ),
            click.option("--quiet", is_flag=True),
        ]
    )
    for option in reversed(options):
        command = option(command)
    return command


def email_flags(command):
    """Recipients and provider settings shared by review and notify commands."""
    options = [
        click.option(
            "--email-to",
            multiple=True,
            help=(
                "Bundle passed or uncertain, previously unemailed flats for this address; "
                "repeat for more recipients."
            ),
        ),
        click.option(
            "--email-preview",
            type=click.Path(dir_okay=False, path_type=Path),
            help=(
                "Preview eligible passed/uncertain flats for one recipient without sending "
                "or marking delivery."
            ),
        ),
        click.option(
            "--env-file",
            type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
            help="Read email credentials from this file; uses .env if present.",
        ),
        click.option("--email-timeout", type=float, default=30, show_default=True),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def review_flags(*, required=False):
    """Shared headless review settings for one-off and scheduled jobs."""

    def decorate(command):
        options = [
            click.option(
                "--criteria-file",
                required=required,
                type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
                help="UTF-8 suitability conditions; enables Agents SDK review.",
            ),
            click.option(
                "--review-backend",
                type=click.Choice(["codex", "responses"]),
                envvar="OPENRENT_REVIEW_BACKEND",
                default="codex",
                show_default=True,
                help="Agents SDK backend used for suitability review.",
            ),
            click.option(
                "--review-model",
                envvar="OPENRENT_REVIEW_MODEL",
                help="Model name for the selected backend; must support images and structured output.",
            ),
            click.option("--review-timeout", type=float, default=300, show_default=True),
            click.option(
                "--review-concurrency", type=click.IntRange(1, 4), default=1, show_default=True
            ),
            click.option(
                "--review-limit",
                type=click.IntRange(min=1),
                help=(
                    "Maximum properties in this cycle; email after every selected review completes."
                ),
            ),
            click.option(
                "--stop-after-pass",
                is_flag=True,
                help=(
                    "Stop a review-only batch after its first pass; "
                    "requires concurrency 1 and no email flags."
                ),
            ),
        ]
        for option in reversed(options):
            command = option(command)
        return email_flags(command)

    return decorate


def scan_options(args):
    """Validate before opening a client or entering the daemon loop."""
    if args.post_filter and args.dry_run:
        raise SearchError("--filter requires saved detail metadata; omit --dry-run.")
    if args.radius_distance is None and args.radius_minutes is None:
        raise SearchError("Choose exactly one of --radius-distance or --radius-minutes.")
    values = vars(args) | {"property_types": args.property_types or None}
    return tuple(
        model(**{name: values[name] for name in model.__dataclass_fields__})
        for model in (ApiFilters, WebsiteFilters)
    )


async def fetch(args):
    api, website = scan_options(args)
    async with OpenRentClient(
        timeout=args.timeout,
        concurrency=args.concurrency,
        requests_per_second=args.requests_per_second,
        cookie_file=args.cookie_file,
    ) as client:
        fresh = await discover(args, client, None, api, website)
        if args.dry_run:
            return 0
        async with AsyncDatabase(args.db) as db, DownloadJobs(args.db) as jobs:
            for item in fresh.values():
                await jobs.enqueue(item, refresh=args.refresh)
            return await drain(args, client, db, jobs, fresh)


async def daemon(args):
    from .daemon import run_daemon, run_pipeline, validate_schedule

    api, website = scan_options(args)
    validate_schedule(args.cron, args.timezone)
    if args.review_cron:
        validate_schedule(args.review_cron, args.timezone)
        if args.criteria_file is None:
            raise SearchError("--review-cron requires --criteria-file.")
    if args.check_schedule:
        return await run_daemon(args, None)

    review_config = None
    if (
        (args.email_to or args.email_preview)
        and args.criteria_file is None
        and not args.check_schedule
    ):
        raise SearchError("Daemon email delivery requires --criteria-file to enable review.")
    if args.criteria_file is not None and not args.check_schedule:
        from .review import configuration

        if args.skip_images:
            raise SearchError("Suitability review needs downloaded images; omit --skip-images.")
        review_config = configuration(args)

    if args.dry_run:
        return await run_daemon(args, fetch)

    async with (
        OpenRentClient(
            timeout=args.timeout,
            concurrency=args.concurrency,
            requests_per_second=args.requests_per_second,
            cookie_file=args.cookie_file,
        ) as client,
        AsyncDatabase(args.db) as db,
        DownloadJobs(args.db) as jobs,
    ):
        fresh = {}

        async def discovery():
            fresh.update(await discover(args, client, jobs, api, website))
            return 0

        async def downloads():
            current = fresh.copy()
            fresh.clear()
            return await drain(args, client, db, jobs, current)

        async def reviews():
            from .review import process_pending

            return await process_pending(review_config, quiet=args.quiet, ready_only=True)

        return await run_pipeline(
            args, discovery, downloads, reviews if review_config is not None else None
        )


class InterruptedScan(click.ClickException):
    exit_code = 130


def _invoke(command):
    """Run a Click callback with shared async execution and error reporting."""

    @wraps(command)
    def invoke(**parameters):
        try:
            result = command(SimpleNamespace(**parameters))
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
        except (FetchError, ValueError, sqlite3.Error, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        except KeyboardInterrupt as exc:
            raise InterruptedScan(
                "Interrupted; saved properties and images can be resumed with the same command."
            ) from exc
        click.get_current_context().exit(result or 0)

    return invoke


@click.group(name="openrent", context_settings={"help_option_names": ["--help", "-h"]})
def app():
    """Fetch OpenRent listings and images into SQLite."""


@app.command("fetch")
@scan_flags
@_invoke
def fetch_command(args):
    """Search and import listings with all listing images."""
    return fetch(args)


@app.command("daemon")
@scan_flags
@review_flags()
@click.option("--cron", required=True, help="Quoted five-field UNIX cron expression.")
@click.option("--review-cron", help="Independent review schedule; defaults to --cron.")
@click.option(
    "--retry-interval",
    type=click.FloatRange(min=0, min_open=True),
    default=60,
    show_default=True,
    help="Seconds between passes retrying unfinished downloads.",
)
@click.option("--timezone", default="Europe/London", help="IANA schedule timezone.")
@click.option("--run-now", is_flag=True, help="Discover and review immediately on startup.")
@click.option(
    "--max-runs",
    type=click.IntRange(min=1),
    help="Stop after N discoveries and a final download/review pass.",
)
@click.option(
    "--check-schedule", is_flag=True, help="Print the next five discovery times without scanning."
)
@_invoke
def daemon_command(args):
    """Schedule discovery and review while continuously downloading queued work."""
    return daemon(args)


@app.command("review")
@review_flags(required=True)
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
@click.option(
    "--dry-run",
    is_flag=True,
    help="List live unprocessed IDs in this cycle without downloading images or calling the model.",
)
@click.option("--quiet", is_flag=True)
@click.option("--cron", help="Optional quoted five-field schedule for checking the archive.")
@click.option("--timezone", default="Europe/London")
@click.option("--run-now", is_flag=True)
@click.option("--max-runs", type=click.IntRange(min=1))
@click.option("--check-schedule", is_flag=True)
@_invoke
def review_command_cli(args):
    """Review live unprocessed properties once with the Agents SDK.

    Download missing archived gallery images before judging. By default, include
    new arrivals until the queue is complete; --review-limit bounds the cycle.

    With --email-to, bundle passed/uncertain flats after every selected review
    completes. An empty queue or incomplete selected batch sends no email.
    """
    from .review import review_command

    return review_command(args)


@app.command("notify")
@email_flags
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
@click.option("--quiet", is_flag=True)
@_invoke
def notify_command_cli(args):
    """Send or preview unemailed passed/uncertain stored reviews without another model run."""
    from .notifications import notify_command

    return notify_command(args)


@app.command("review-schema")
@_invoke
def review_schema_command(args):
    """Print the strict JSON output schema sent to the review model."""
    import json

    from .judge import judgement_schema

    click.echo(json.dumps(judgement_schema(), indent=2, ensure_ascii=False))


@app.command("stats")
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
@_invoke
def stats_command(args):
    """Show stored listing and image counts."""
    from .review_db import ReviewDatabase

    if not args.db.is_file():
        raise SearchError(f"Database does not exist: {args.db}")
    with ReviewDatabase(args.db) as db:
        for key, value in db.counts().items():
            click.echo(f"{key}: {value}")


@app.command("schema")
@_invoke
def schema_command(args):
    """Print the normalized SQLite schema."""
    click.echo(files("openrent").joinpath("schema.sql").read_text())


def main(argv=None):
    """Entry point returning exit codes, including the flags-only script invocation."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0].startswith("--") and arguments[0] not in ("--help", "-h"):
        arguments.insert(0, "fetch")
    try:
        result = app.main(args=arguments, prog_name="openrent", standalone_mode=False)
        return 0 if result is None else result
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
