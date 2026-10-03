"""uv-run command line interface and resilient, repeatable import orchestration."""

import asyncio
import math
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import click

from .async_db import AsyncDatabase
from .client import BASE_URL, FetchError, OpenRentClient
from .models import Candidate
from .parsing import enrich_summary, parse_property, parse_search
from .search import ApiFilters, SearchError, WebsiteFilters, matches_criteria


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


def column_names(value):
    columns = [column.strip() for column in value.split(",")]
    if not all(columns) or len(columns) != len(set(columns)):
        raise ValueError("Use unique, non-empty comma-separated column names.")
    return columns


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
            "--email-provider",
            type=click.Choice(["resend", "cloudflare"]),
            default="resend",
            show_default=True,
            help="Email sending service; Resend reads RESEND_TOKEN.",
        ),
        click.option(
            "--env-file",
            type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
            help="Read email credentials from this file; uses .env if present.",
        ),
        click.option(
            "--cloudflare-account-id",
            envvar="CLOUDFLARE_ACCOUNT_ID",
            help="Cloudflare account ID; token comes from CLOUDFLARE_API_TOKEN.",
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


def log(args, message):
    if not args.quiet:
        print(message, file=sys.stderr, flush=True)


def scan_options(args):
    """Validate before opening a client or entering the daemon loop."""
    if getattr(args, "post_filter", False) and args.dry_run:
        raise SearchError("--filter requires saved detail metadata; omit --dry-run.")
    if not 1 <= args.concurrency <= 32:
        raise SearchError("--concurrency must be between 1 and 32.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise SearchError("Use --timeout > 0.")
    if not math.isfinite(args.requests_per_second) or args.requests_per_second <= 0:
        raise SearchError("Use --requests-per-second > 0 with a finite value.")
    if not args.location or (args.radius_distance is None) == (args.radius_minutes is None):
        raise SearchError(
            "Supply --location and exactly one of --radius-distance or --radius-minutes."
        )
    values = vars(args) | {"property_types": args.property_types or None}
    return tuple(
        model(**{name: values[name] for name in model.__dataclass_fields__})
        for model in (ApiFilters, WebsiteFilters)
    )


async def gather_tasks(coroutines):
    """Join tasks before closing shared resources, including on failure or cancellation."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def fetch(args):
    api, website = scan_options(args)
    async with OpenRentClient(
        timeout=args.timeout,
        concurrency=args.concurrency,
        requests_per_second=args.requests_per_second,
        cookie_file=args.cookie_file,
    ) as client:
        log(args, f"Searching {api.location} ...")
        response = await client.search(api.parameters())
        reference_date = datetime.now(ZoneInfo("Europe/London")).date()
        search = await asyncio.to_thread(
            parse_search, response.text, str(response.url), reference_date=reference_date
        )
        if api.radius_minutes is not None and "minute" not in (search.distance_unit or "").lower():
            raise SearchError(
                "OpenRent returned distance results for this commute request. "
                "Its travel-time search supports London locations; use --radius-distance elsewhere."
            )
        candidates = [
            candidate
            for candidate in search.candidates
            if matches_criteria(api, website, candidate, search)
        ]
        if args.sort == "rent-asc":
            candidates.sort(key=lambda c: c.property.rent_pcm_pence or 0)
        elif args.sort == "rent-desc":
            candidates.sort(key=lambda c: c.property.rent_pcm_pence or 0, reverse=True)
        elif args.sort == "newest":
            candidates.sort(key=lambda c: c.property.first_listed_at or "", reverse=True)
        else:
            candidates.sort(
                key=lambda c: c.commute_minutes if api.radius_minutes is not None else c.distance_km
            )
        log(
            args,
            f"{len(search.candidates)} candidates; {len(candidates)} match; importing all matches.",
        )
        if args.dry_run:
            print(f"{len(candidates)} matches; database unchanged.")
            for candidate in candidates:
                prop = candidate.property
                radius = (
                    f"{candidate.commute_minutes:g} min"
                    if api.radius_minutes is not None
                    else f"{candidate.distance_km:.2f} km"
                )
                print(
                    f"{prop.id}\t£{prop.rent_pcm_pence / 100:.2f}/month\t{radius}\t{BASE_URL}/{prop.id}"
                )
            return 0

        new_properties = downloaded = errors = imported = filtered_out = 0
        seen = []
        async with AsyncDatabase(args.db) as db:

            async def download(property_id, picture):
                nonlocal downloaded, errors
                if await db.image_downloaded(property_id, picture.source_url):
                    return
                try:
                    content, mime, dimensions, headers = await client.image(picture.source_url)
                except FetchError as exc:
                    errors += 1
                    await db.record_image_error(property_id, picture.source_url, str(exc))
                    log(args, f"{property_id} image: {exc}")
                    return
                picture.width, picture.height = dimensions
                stored = await db.store_image(
                    property_id,
                    picture,
                    content,
                    mime,
                    etag=headers.get("etag"),
                    last_modified=headers.get("last-modified"),
                )
                downloaded += stored

            async def load(candidate, summaries):
                nonlocal new_properties, imported, errors
                try:
                    prop = candidate.property
                    if prop.id in summaries:
                        try:
                            enrich_summary(candidate, summaries[prop.id])
                        except ValueError as exc:
                            log(args, f"{prop.id}: invalid summary; using detail page: {exc}")
                    detail = await client.get(BASE_URL + f"/{prop.id}")
                    prop = await asyncio.to_thread(
                        parse_property, detail.text, str(detail.url), candidate
                    )
                    if args.no_source_html:
                        prop.source_html = None
                    current = Candidate(prop, candidate.distance_km, candidate.commute_minutes)
                    matches = matches_criteria(api, website, current, search)
                except (FetchError, ValueError) as exc:
                    errors += 1
                    log(args, f"{candidate.property.id}: {exc}")
                    return
                if not matches:
                    if await db.get_property(prop.id):
                        await db.upsert_property(prop)
                    log(args, f"{prop.id}: no longer matches after refreshing details.")
                    return
                inserted = await db.upsert_property(prop)
                new_properties += inserted
                imported += 1
                seen.append(prop.id)
                log(args, f"[{imported}/{len(candidates)}] {prop.id}: {prop.title}")
                if not args.skip_images:
                    await gather_tasks(download(prop.id, picture) for picture in prop.images)

            async def load_group(group):
                try:
                    summaries = {
                        int(s["id"]): s
                        for s in await client.summaries([c.property.id for c in group])
                    }
                except (FetchError, ValueError, TypeError) as exc:
                    log(args, f"Summary API unavailable; fetching detail pages: {exc}")
                    summaries = {}
                await gather_tasks(load(candidate, summaries) for candidate in group)

            await gather_tasks(
                load_group(candidates[start : start + 20])
                for start in range(0, len(candidates), 20)
            )
            if getattr(args, "post_filter", False):
                kept = await db.filter_properties(seen)
                filtered_out = len(seen) - len(kept)
                log(args, f"Post-filter kept {len(kept)} listings; deleted {filtered_out}.")
            counts = await db.counts()
        filter_summary = (
            f"Post-filter deleted {filtered_out}. " if getattr(args, "post_filter", False) else ""
        )
        print(
            f"Imported {imported} properties ({new_properties} new); downloaded {downloaded} images; "
            f"{errors} errors. {filter_summary}Database: {args.db.resolve()} "
            f"({counts['properties']} properties, {counts['downloaded_images']} stored images).",
            flush=True,
        )
        return 1 if errors else 0


async def daemon(args):
    from .daemon import run_daemon, validate_schedule
    from .export import available_columns, export_csv, validate_destination

    scan_options(args)
    validate_schedule(args.cron, args.timezone)
    if args.max_runs is not None and args.max_runs <= 0:
        raise SearchError("--max-runs must be positive.")
    if args.export_columns and not args.export_csv:
        raise SearchError("--export-columns requires --export-csv.")
    if args.export_columns:
        unknown = set(args.export_columns) - set(available_columns())
        if unknown:
            raise SearchError(f"Unknown export columns: {', '.join(sorted(unknown))}")
    if args.export_csv:
        validate_destination(args.db, args.export_csv)
    if args.export_csv and args.dry_run:
        raise SearchError("--export-csv cannot be combined with --dry-run.")

    review_config = None
    if (
        (getattr(args, "email_to", ()) or getattr(args, "email_preview", None))
        and getattr(args, "criteria_file", None) is None
        and not args.check_schedule
    ):
        raise SearchError("Daemon email delivery requires --criteria-file to enable review.")
    if getattr(args, "criteria_file", None) is not None and not args.check_schedule:
        from .review import configuration

        if args.skip_images:
            raise SearchError("Suitability review needs downloaded images; omit --skip-images.")
        review_config = configuration(args)

    async def scan(scan_args):
        result = await fetch(scan_args)
        if result == 0 and args.export_csv:
            export = asyncio.create_task(
                asyncio.to_thread(
                    export_csv,
                    args.db,
                    args.export_csv,
                    columns=args.export_columns,
                    active_only=True,
                )
            )
            try:
                rows = await asyncio.shield(export)
            except asyncio.CancelledError:
                # Finish atomic replacement even when stopping during export.
                await export
                raise
            log(args, f"Exported {rows} live archived listings to {args.export_csv.resolve()}.")
        if result == 0 and review_config is not None and not args.dry_run:
            from .review import process_pending

            result = await process_pending(review_config, quiet=args.quiet)
        return result

    return await run_daemon(args, scan)


def _dispatch(args):
    """Run an already parsed command, keeping scanning separate from Click."""
    if args.command == "fetch":
        return asyncio.run(fetch(args))
    if args.command == "daemon":
        return asyncio.run(daemon(args))
    if args.command == "review":
        from .review import review_command

        return asyncio.run(review_command(args))
    if args.command == "notify":
        from .notifications import notify_command

        return asyncio.run(notify_command(args))
    if args.command == "export":
        from .export import available_columns, export_csv

        if args.list_columns:
            click.echo("\n".join(available_columns()))
            return 0
        if args.output is None:
            raise SearchError("export requires --output unless --list-columns is used.")
        count = export_csv(args.db, args.output, args.columns, args.active_only)
        click.echo(f"Exported {count} properties to {args.output.resolve()}.")
        return 0
    if args.command == "schema":
        click.echo(files("openrent").joinpath("schema.sql").read_text())
    else:
        if not args.db.is_file():
            raise SearchError(f"Database does not exist: {args.db}")
        from .review_db import ReviewDatabase

        with ReviewDatabase(args.db) as db:
            for key, value in db.counts().items():
                click.echo(f"{key}: {value}")
    return 0


class InterruptedScan(click.ClickException):
    exit_code = 130


def _invoke(command, parameters):
    if command in {"fetch", "daemon"} and (
        (parameters["radius_distance"] is None) == (parameters["radius_minutes"] is None)
    ):
        raise click.UsageError("Choose exactly one of --radius-distance or --radius-minutes.")
    args = SimpleNamespace(command=command, **parameters)
    try:
        result = _dispatch(args)
    except (FetchError, SearchError, ValueError, sqlite3.Error, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt as exc:
        raise InterruptedScan(
            "Interrupted; saved properties and images can be resumed with the same command."
        ) from exc
    click.get_current_context().exit(result)


@click.group(name="openrent", context_settings={"help_option_names": ["--help", "-h"]})
def app():
    """Fetch OpenRent listings and images into SQLite."""


@app.command("fetch")
@scan_flags
def fetch_command(**parameters):
    """Search and import listings with all listing images."""
    _invoke("fetch", parameters)


@app.command("daemon")
@scan_flags
@review_flags()
@click.option("--cron", required=True, help="Quoted five-field UNIX cron expression.")
@click.option("--timezone", default="Europe/London", help="IANA schedule timezone.")
@click.option("--run-now", is_flag=True, help="Scan immediately on startup.")
@click.option("--max-runs", type=int, help="Stop after this many scan attempts.")
@click.option(
    "--check-schedule", is_flag=True, help="Print the next five run times without scanning."
)
@click.option(
    "--export-csv",
    type=click.Path(path_type=Path),
    help="Refresh a CSV of live archived listings after successful scans.",
)
@click.option(
    "--export-columns",
    type=column_names,
    help="Comma-separated CSV fields; use export --list-columns to inspect.",
)
def daemon_command(**parameters):
    """Run repeated scans on a five-field cron schedule."""
    _invoke("daemon", parameters)


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
def review_command_cli(**parameters):
    """Review live unprocessed properties once with the Agents SDK.

    Download missing archived gallery images before judging. By default, include
    new arrivals until the queue is complete; --review-limit bounds the cycle.

    With --email-to, bundle passed/uncertain flats after every selected review
    completes. An empty queue or incomplete selected batch sends no email.
    """
    _invoke("review", parameters)


@app.command("notify")
@email_flags
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
@click.option("--quiet", is_flag=True)
def notify_command_cli(**parameters):
    """Send or preview unemailed passed/uncertain stored reviews without another model run."""
    _invoke("notify", parameters)


@app.command("review-schema")
def review_schema_command():
    """Print the strict JSON output schema sent to the review model."""
    import json

    from .judge import judgement_schema

    click.echo(json.dumps(judgement_schema(), indent=2, ensure_ascii=False))


@app.command("export")
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
@click.option("--output", type=click.Path(path_type=Path), help="CSV file; replaced atomically.")
@click.option("--columns", type=column_names, help="Comma-separated fields to export.")
@click.option(
    "--active-only",
    is_flag=True,
    help="Only listings disclosed as live.",
)
@click.option("--list-columns", is_flag=True, help="List available fields.")
def export_command(**parameters):
    """Export a selected set of SQLite fields to CSV."""
    _invoke("export", parameters)


@app.command("stats")
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
def stats_command(**parameters):
    """Show stored listing and image counts."""
    _invoke("stats", parameters)


@app.command("schema")
def schema_command():
    """Print the normalized SQLite schema."""
    _invoke("schema", {})


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
