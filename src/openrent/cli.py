"""uv-run command line interface and resilient, repeatable import orchestration."""

import asyncio
import math
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import click

from .async_db import AsyncDatabase
from .client import BASE_URL, FetchError, OpenRentClient
from .db import Database
from .models import Candidate
from .parsing import enrich_summary, parse_property, parse_search
from .search import SearchError, SearchOptions


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
            "--furnishing", type=click.Choice(["any", "furnished", "unfurnished"]), default="any"
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
        options.append(click.option("--" + name, is_flag=True, help=help_text))
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
            click.option(
                "--limit",
                "--max-properties",
                type=int,
                help="Limit detail imports; default imports all matches.",
            ),
            click.option("--skip-images", is_flag=True, help="Save image metadata without bytes."),
            click.option(
                "--no-source-html", is_flag=True, help="Omit the sanitized listing HTML backup."
            ),
            click.option("--dry-run", is_flag=True, help="Show matches without creating a DB."),
            click.option(
                "--delay", type=float, default=0.5, help="Seconds between requests (default 0.5)."
            ),
            click.option(
                "--concurrency",
                type=click.IntRange(1, 32),
                default=4,
                show_default=True,
                help="Maximum simultaneous HTTP requests; pacing applies across all workers.",
            ),
            click.option("--timeout", type=float, default=30),
            click.option("--retries", type=int, default=3),
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


def log(args, message):
    if not args.quiet:
        print(message, file=sys.stderr, flush=True)


def scan_options(args):
    """Validate before opening a client or entering the daemon loop."""
    if args.limit is not None and args.limit <= 0:
        raise SearchError("--limit must be positive.")
    if not 1 <= args.concurrency <= 32:
        raise SearchError("--concurrency must be between 1 and 32.")
    if (
        not math.isfinite(args.delay)
        or not math.isfinite(args.timeout)
        or args.delay < 0
        or args.timeout <= 0
        or args.retries < 0
    ):
        raise SearchError("Use --delay >= 0, --timeout > 0, and --retries >= 0.")
    names = SearchOptions.__dataclass_fields__
    return SearchOptions(
        **{
            key: tuple(args.property_types or ()) if key == "property_types" else getattr(args, key)
            for key in names
        }
    )


@asynccontextmanager
async def bounded_results(items, operation, concurrency):
    """Stream results from a fixed number of workers with bounded buffering.

    The context cancels and joins workers before its caller can close the HTTP
    client or database, including when a write fails or the scan is interrupted.
    """
    pending = iter(items)
    results = asyncio.Queue(maxsize=1)
    done = object()

    async def worker():
        for item in pending:
            await results.put(await operation(item))
        await results.put(done)

    async def consume():
        finished = 0
        while finished < concurrency:
            result = await results.get()
            if result is done:
                finished += 1
            else:
                yield result

    try:
        async with asyncio.TaskGroup() as group:
            workers = [group.create_task(worker()) for _ in range(concurrency)]
            try:
                yield consume()
            finally:
                for task in workers:
                    task.cancel()
    except ExceptionGroup as exc:
        # TaskGroup wraps even a single failure from the consumer (e.g. a
        # SQLite write). Preserve that exception for the CLI's error handling.
        if len(exc.exceptions) == 1:
            raise exc.exceptions[0] from exc
        raise


async def fetch(args):
    options = scan_options(args)
    async with OpenRentClient(
        args.delay, args.timeout, args.retries, args.cookie_file, concurrency=args.concurrency
    ) as client:
        log(args, f"Searching {options.location} ...")
        response = await client.search(options.parameters())
        reference_date = datetime.now(ZoneInfo("Europe/London")).date()
        search = await asyncio.to_thread(
            parse_search, response.text, str(response.url), reference_date=reference_date
        )
        if (
            options.radius_minutes is not None
            and "minute" not in (search.distance_unit or "").lower()
        ):
            raise SearchError(
                "OpenRent returned distance results for this commute request. "
                "Its travel-time search supports London locations; use --radius-distance elsewhere."
            )
        candidates = [
            candidate for candidate in search.candidates if options.matches(candidate, search)
        ]
        if args.sort == "rent-asc":
            candidates.sort(key=lambda c: c.property.rent_pcm_pence or 0)
        elif args.sort == "rent-desc":
            candidates.sort(key=lambda c: c.property.rent_pcm_pence or 0, reverse=True)
        elif args.sort == "newest":
            candidates.sort(key=lambda c: c.property.first_listed_at or "", reverse=True)
        else:
            candidates.sort(
                key=lambda c: (
                    c.commute_minutes if options.radius_minutes is not None else c.distance_km
                )
            )
        matched = len(candidates)
        selected = candidates[: args.limit] if args.limit else candidates
        log(
            args,
            f"{len(search.candidates)} candidates; {matched} match; importing {len(selected)}.",
        )
        if args.dry_run:
            print(f"{matched} matches ({len(selected)} shown); database unchanged.")
            for candidate in selected:
                prop = candidate.property
                radius = (
                    f"{candidate.commute_minutes:g} min"
                    if options.radius_minutes is not None
                    else f"{candidate.distance_km:.2f} km"
                )
                print(
                    f"{prop.id}\t£{prop.rent_pcm_pence / 100:.2f}/month\t{radius}\t{BASE_URL}/{prop.id}"
                )
            return 0

        new_properties = downloaded = errors = imported = 0
        seen = []
        async with AsyncDatabase(args.db) as db:
            search_id = await db.upsert_search(
                options.identity_parameters(), options.location, search.latitude, search.longitude
            )
            for start in range(0, len(selected), 20):
                batch = selected[start : start + 20]
                try:
                    summaries = {
                        int(s["id"]): s
                        for s in await client.summaries([c.property.id for c in batch])
                    }
                except (FetchError, ValueError, TypeError) as exc:
                    # Details remain a usable public source if the site's
                    # undocumented summary endpoint is temporarily unavailable.
                    log(args, f"Summary API unavailable; fetching detail pages: {exc}")
                    summaries = {}

                async def load(candidate, summaries=summaries):
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
                        matches = options.matches(current, search)
                        return candidate, current, matches, None
                    except (FetchError, ValueError) as exc:
                        return candidate, None, False, exc

                pictures = []
                async with bounded_results(batch, load, args.concurrency) as results:
                    async for candidate, current, matches, error in results:
                        if error is not None:
                            errors += 1
                            log(args, f"{candidate.property.id}: {error}")
                            continue
                        prop = current.property
                        # Recheck refreshed facts in case a listing changed
                        # between the search and detail requests.
                        if not matches:
                            if await db.get_property(prop.id):
                                await db.upsert_property(prop)
                            log(args, f"{prop.id}: no longer matches after refreshing details.")
                            continue
                        new_properties += await db.upsert_property(prop)
                        await db.record_match(search_id, current)
                        imported += 1
                        seen.append(prop.id)
                        log(args, f"[{imported}/{len(selected)}] {prop.id}: {prop.title}")
                        if not args.skip_images:
                            for picture in prop.images:
                                if not await db.image_downloaded(prop.id, picture.source_url):
                                    pictures.append((prop.id, picture))

                async def download(item):
                    property_id, picture = item
                    try:
                        return property_id, picture, await client.image(picture.source_url), None
                    except FetchError as exc:
                        return property_id, picture, None, exc

                async with bounded_results(pictures, download, args.concurrency) as results:
                    async for property_id, picture, data, error in results:
                        if error is not None:
                            errors += 1
                            await db.record_image_error(property_id, picture.source_url, str(error))
                            log(args, f"{property_id} image: {error}")
                            continue
                        content, mime, dimensions, headers = data
                        picture.width, picture.height = dimensions
                        downloaded += await db.store_image(
                            property_id,
                            picture,
                            content,
                            mime,
                            etag=headers.get("etag"),
                            last_modified=headers.get("last-modified"),
                        )
            complete = len(selected) == matched and errors == 0
            await db.finish_search(search_id, seen, complete=complete)
            counts = await db.counts()
        print(
            f"Imported {imported} properties ({new_properties} new); downloaded {downloaded} images; "
            f"{errors} errors. Database: {args.db.resolve()} "
            f"({counts['properties']} properties, {counts['downloaded_images']} stored images).",
            flush=True,
        )
        return 1 if errors else 0


async def daemon(args):
    from .daemon import run_daemon, validate_schedule
    from .export import available_columns, export_csv, validate_destination
    from .locking import ScanLock

    options = scan_options(args)
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
    if args.export_csv and args.limit is not None:
        raise SearchError("Scheduled CSV export requires a full search; omit --limit.")

    async def scan(scan_args):
        result = await fetch(scan_args)
        if result == 0 and args.export_csv:
            export = asyncio.create_task(
                asyncio.to_thread(
                    export_csv,
                    args.db,
                    args.export_csv,
                    columns=args.export_columns,
                    search_id=Database.search_id_for(
                        options.identity_parameters(), options.location
                    ),
                    active_only=True,
                )
            )
            try:
                rows = await asyncio.shield(export)
            except asyncio.CancelledError:
                # Atomic file replacement must finish before the scan lock is
                # released, even when stopping during a scheduled export.
                await export
                raise
            log(args, f"Exported {rows} current search matches to {args.export_csv.resolve()}.")
        return result

    if args.check_schedule or args.dry_run:
        return await run_daemon(args, scan)
    with ScanLock(args.db):
        return await run_daemon(args, scan)


def _dispatch(args):
    """Run an already parsed command, keeping scanning separate from Click."""
    if args.command == "fetch":
        if args.dry_run:
            return asyncio.run(fetch(args))
        from .locking import ScanLock

        with ScanLock(args.db):
            return asyncio.run(fetch(args))
    if args.command == "daemon":
        return asyncio.run(daemon(args))
    if args.command == "export":
        from .export import available_columns, export_csv

        if args.list_columns:
            click.echo("\n".join(available_columns()))
            return 0
        if args.output is None:
            raise SearchError("export requires --output unless --list-columns is used.")
        count = export_csv(args.db, args.output, args.columns, args.search_id, args.active_only)
        click.echo(f"Exported {count} properties to {args.output.resolve()}.")
        return 0
    if args.command == "schema":
        click.echo(files("openrent").joinpath("schema.sql").read_text())
    else:
        if not args.db.is_file():
            raise SearchError(f"Database does not exist: {args.db}")
        with Database(args.db) as db:
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
    help="Refresh a CSV of current search matches after successful scans.",
)
@click.option(
    "--export-columns",
    type=column_names,
    help="Comma-separated CSV fields; use export --list-columns to inspect.",
)
def daemon_command(**parameters):
    """Run repeated scans on a five-field cron schedule."""
    _invoke("daemon", parameters)


@app.command("export")
@click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite"))
@click.option("--output", type=click.Path(path_type=Path), help="CSV file; replaced atomically.")
@click.option("--columns", type=column_names, help="Comma-separated fields to export.")
@click.option("--search-id", help="Restrict to a specific stored search.")
@click.option(
    "--active-only",
    is_flag=True,
    help="Only active search matches, or live listings if no search selected.",
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
