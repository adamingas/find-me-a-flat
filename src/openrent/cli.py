"""Click entry point for the DBOS service."""

import asyncio
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import click

from .client import FetchError
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
    """Filters for scheduled discovery."""
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
                "--filter/--no-filter",
                "post_filter",
                default=False,
                help="Require a Tube walk of at most 11 minutes and EPC A/B/C before downloading images.",
            ),
        ]
    )
    for option in reversed(options):
        command = option(command)
    return command


def request_flags(command):
    for option in reversed(
        [
            click.option("--db", type=click.Path(path_type=Path), default=Path("openrent.sqlite")),
            click.option(
                "--requests-per-minute",
                "--rpm",
                type=click.FloatRange(min=0, min_open=True),
                default=12,
                show_default=True,
                help="OpenRent request starts per minute, including retries.",
            ),
            click.option("--concurrency", type=click.IntRange(min=1), default=1, show_default=True),
            click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=30),
            click.option("--cookie-file", type=click.Path(path_type=Path)),
            click.option("--quiet", is_flag=True),
        ]
    ):
        command = option(command)
    return command


def email_flags(command):
    """Email delivery settings for reviewed results."""
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
        click.option("--email-timeout", type=float, default=30, show_default=True),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def env_file_flag(command):
    return click.option(
        "--env-file",
        type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
        help="Load credentials once at startup; uses .env if present. Process variables override it.",
    )(command)


def review_flags(*, required=False):
    """Review model settings."""

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
            click.option(
                "--review-api",
                type=click.Choice(["openai", "openrouter"]),
                default="openai",
                show_default=True,
                envvar="OPENRENT_REVIEW_API",
                help="Model API used by the responses backend.",
            ),
            click.option(
                "--review-provider",
                envvar="OPENRENT_REVIEW_PROVIDER",
                help="OpenRouter provider slug, e.g. relace; pins routing without fallback.",
            ),
            click.option("--review-timeout", type=float, default=300, show_default=True),
            click.option(
                "--review-concurrency", type=click.IntRange(min=1), default=1, show_default=True
            ),
        ]
        for option in reversed(options):
            command = option(command)
        return env_file_flag(command)

    return decorate


def scan_options(args):
    """Validate before opening a client or entering the daemon loop."""
    if args.radius_distance is None and args.radius_minutes is None:
        raise SearchError("Choose exactly one of --radius-distance or --radius-minutes.")
    values = vars(args) | {"property_types": args.property_types or None}
    return tuple(
        model(**{name: values[name] for name in model.__dataclass_fields__})
        for model in (ApiFilters, WebsiteFilters)
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
    """Discover, archive, review and notify through durable DBOS workflows."""


@app.command("daemon")
@scan_flags
@request_flags
@review_flags()
@email_flags
@click.option(
    "--notify-cron-schedule",
    help="Independent five-field cron schedule for emailing completed reviews.",
)
@click.option("--cron", required=True, help="Five-field cron schedule for discovering properties.")
@click.option("--timezone", default="Europe/London", help="IANA schedule timezone.")
@click.option("--run-now", is_flag=True, help="Discover immediately on startup as well.")
@_invoke
def daemon_command(args):
    """Schedule discovery and notification; continuously download and review."""
    from .daemon import run_daemon

    return run_daemon(args, *scan_options(args))


def main():
    """Click entry point returning an exit code."""
    try:
        result = app.main(prog_name="openrent", standalone_mode=False)
        return 0 if result is None else result
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
