"""CLI integration, real process locks, and scheduled shared-archive exports."""

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openrent import cli
from openrent.daemon import run_daemon
from openrent.db import Database
from openrent.locking import ScanLock
from openrent.models import Property


def arguments(db, *extra):
    return [
        "daemon",
        "--location",
        "Victoria, London",
        "--radius-distance",
        "2",
        "--cron",
        "*/15 * * * *",
        "--db",
        str(db),
        *extra,
    ]


def test_schedule_preview_does_not_create_db_or_lock_or_scan(tmp_path, monkeypatch, capsys):
    async def forbidden(_):
        raise AssertionError("Preview must not perform an HTTP scan")

    monkeypatch.setattr(cli, "fetch", forbidden)
    db = tmp_path / "absent" / "archive.sqlite"
    assert cli.main(arguments(db, "--check-schedule")) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 5
    assert not db.parent.exists()


def test_lock_excludes_other_instances_and_releases_on_error(tmp_path):
    db = tmp_path / "archive.sqlite"
    with ScanLock(db), pytest.raises(ValueError, match="database lock"), ScanLock(db):
        pass
    with pytest.raises(RuntimeError), ScanLock(db):
        raise RuntimeError("simulated scan failure")
    with ScanLock(db):
        assert Path(str(db) + ".scan.lock").exists()


def test_lock_normalizes_symlink_database_paths(tmp_path):
    db = tmp_path / "archive.sqlite"
    db.touch()
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(db)
    with ScanLock(db), pytest.raises(ValueError, match="database lock"), ScanLock(alias):
        pass


def test_scanners_do_not_take_a_database_wide_process_lock(tmp_path, monkeypatch):
    calls = []

    async def scan(args):
        calls.append(args.location)
        return 0

    monkeypatch.setattr(cli, "fetch", scan)
    db = tmp_path / "archive.sqlite"
    # An existing legacy lock no longer blocks manual or daemon ingestion.
    with ScanLock(db):
        assert (
            cli.main(["fetch", "--location", "London", "--radius-distance", "2", "--db", str(db)])
            == 0
        )
        assert cli.main(arguments(db, "--run-now", "--max-runs", "1")) == 0
    assert calls == ["London", "Victoria, London"]


def test_successful_daemon_scan_exports_shared_archive_once(tmp_path, monkeypatch):
    db = tmp_path / "archive.sqlite"
    output = tmp_path / "listings.csv"
    calls = []

    async def scan(args):
        calls.append(args.location)
        with Database(args.db) as archive:
            archive.upsert_property(
                Property(999, "https://www.openrent.co.uk/999", title="Other", is_live=True)
            )
            prop = Property(
                101,
                "https://www.openrent.co.uk/101",
                title="Example Flat",
                rent_pcm_pence=123456,
                is_live=True,
            )
            archive.upsert_property(prop)
        return 0

    monkeypatch.setattr(cli, "fetch", scan)
    args = arguments(
        db,
        "--run-now",
        "--max-runs",
        "1",
        "--export-csv",
        str(output),
        "--export-columns",
        "id,title,rent_pcm",
    )
    assert cli.main(args) == 0
    first = output.read_bytes()
    assert cli.main(args) == 0
    assert output.read_bytes() == first
    with output.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {"id": "101", "title": "Example Flat", "rent_pcm": "1234.56"},
        {"id": "999", "title": "Other", "rent_pcm": ""},
    ]
    assert calls == ["Victoria, London", "Victoria, London"]


def test_failed_daemon_scan_preserves_previous_csv(tmp_path, monkeypatch):
    db = tmp_path / "archive.sqlite"
    output = tmp_path / "listings.csv"
    output.write_text("previous snapshot")

    async def failure(_):
        return 1

    monkeypatch.setattr(cli, "fetch", failure)
    assert cli.main(arguments(db, "--run-now", "--max-runs", "1", "--export-csv", str(output))) == 1
    assert output.read_text() == "previous snapshot"


@pytest.mark.parametrize("suffix", ["", ".scan.lock"])
def test_dangerous_export_destination_rejected_before_scan(tmp_path, monkeypatch, suffix):
    async def forbidden(_):
        pytest.fail("Must reject before scanning")

    monkeypatch.setattr(cli, "fetch", forbidden)
    db = tmp_path / "archive.sqlite"
    output = Path(str(db) + suffix)
    assert cli.main(arguments(db, "--run-now", "--export-csv", str(output))) == 1
    assert not db.exists()


def test_scheduled_export_exception_counts_as_failed_attempt(tmp_path, monkeypatch):
    async def success(_):
        return 0

    monkeypatch.setattr(cli, "fetch", success)
    import openrent.export

    def fail(*args, **kwargs):
        raise OSError("Export failed; previous CSV remains")

    monkeypatch.setattr(openrent.export, "export_csv", fail)

    async def deterministic_run(args, scan):
        return await run_daemon(
            args, scan, now=lambda: datetime(2026, 10, 1, tzinfo=UTC), install_signals=False
        )

    import openrent.daemon

    monkeypatch.setattr(openrent.daemon, "run_daemon", deterministic_run)
    assert (
        cli.main(
            arguments(
                tmp_path / "archive.sqlite",
                "--run-now",
                "--max-runs",
                "1",
                "--export-csv",
                str(tmp_path / "listings.csv"),
            )
        )
        == 1
    )
