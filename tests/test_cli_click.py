"""Click command parsing preserves the scan interface without performing HTTP requests."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openrent import cli
from openrent.client import FetchError


@pytest.fixture
def captured_scan(monkeypatch):
    received = []

    async def capture(args):
        received.append(args)
        return 0

    monkeypatch.setattr(cli, "fetch", capture)
    monkeypatch.setattr(cli, "daemon", capture)
    return received


def scan_flags(*extra):
    return ["--location", "Victoria, London", "--radius-distance", "2", *extra]


def test_fetch_aliases_and_typed_arguments(captured_scan, tmp_path):
    db = tmp_path / "archive.sqlite"
    cookie_file = tmp_path / "cookies.txt"
    result = CliRunner().invoke(
        cli.app,
        [
            "fetch",
            *scan_flags(),
            "--dry-run",
            "--distance-unit",
            "miles",
            "--min-rent",
            "1200",
            "--max-rent",
            "2500.50",
            "--min-bedrooms",
            "1",
            "--max-bedrooms",
            "3",
            "--rps",
            "1.5",
            "--property-type",
            "flat",
            "--property-type",
            "house",
            "--move-in-before",
            "2026-11-30",
            "--db",
            str(db),
            "--cookie-file",
            str(cookie_file),
        ],
    )
    assert result.exit_code == 0, result.output
    args = captured_scan[0]
    assert isinstance(args, SimpleNamespace)
    assert args.location == "Victoria, London"
    assert args.radius_distance == 2.0
    assert args.distance_unit == "miles"
    assert args.rent_min == Decimal(1200)
    assert isinstance(args.rent_min, Decimal)
    assert args.rent_max == Decimal("2500.50")
    assert (args.bedrooms_min, args.bedrooms_max) == (1, 3)
    assert args.requests_per_second == 1.5
    assert tuple(args.property_types) == ("flat", "house")
    assert args.move_in_before == date(2026, 11, 30)
    assert args.db == db
    assert isinstance(args.db, Path)
    assert args.cookie_file == cookie_file
    assert isinstance(args.cookie_file, Path)
    assert not db.exists()
    api, website = cli.scan_options(args)
    assert api.location == args.location and api.radius_km == pytest.approx(3.218688)
    assert website.rent_max == Decimal("2500.50")
    assert website.property_types == ("flat", "house")
    assert website.pets is None and website.furnishing is None
    assert website.include_unavailable is False  # Deliberate CLI default.
    assert set(api.parameters()) == {"term", "searchType", "area"}


@pytest.mark.parametrize(
    "flags, expected",
    [
        (["--radius-distance", "2"], "--location"),
        (
            [
                "--location",
                "Victoria, London",
                "--radius-distance",
                "2",
                "--radius-minutes",
                "15",
            ],
            "radius",
        ),
    ],
)
def test_location_and_one_radius_are_required(flags, expected, captured_scan):
    result = CliRunner().invoke(cli.app, ["fetch", *flags])
    assert result.exit_code == 2
    assert expected in result.output.lower()
    assert not captured_scan


def test_daemon_requires_user_supplied_cron(captured_scan):
    result = CliRunner().invoke(cli.app, ["daemon", *scan_flags()])
    assert result.exit_code == 2
    assert "--cron" in result.output
    assert not captured_scan


def test_flags_only_main_wrapper_defaults_to_fetch(captured_scan):
    assert cli.main(scan_flags("--dry-run", "--rent-max", "2000")) == 0
    assert len(captured_scan) == 1
    assert captured_scan[0].location == "Victoria, London"
    assert captured_scan[0].rent_max == Decimal(2000)


def test_fetch_error_is_reported_with_exit_one(monkeypatch):
    failure = FetchError("OpenRent unavailable")

    async def fail(_):
        raise failure

    monkeypatch.setattr(cli, "fetch", fail)
    result = CliRunner().invoke(cli.app, ["fetch", *scan_flags("--dry-run")])
    assert result.exit_code == 1
    assert f"Error: {failure}" in result.output
    assert "Traceback" not in result.output


def test_interrupted_scan_returns_exit_130(monkeypatch):
    async def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "fetch", interrupt)
    result = CliRunner().invoke(cli.app, ["fetch", *scan_flags("--dry-run")])
    assert result.exit_code == 130
    assert "Interrupted" in result.output
