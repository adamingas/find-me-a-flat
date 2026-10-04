"""CLI integration and process locks for scheduled scans."""

from pathlib import Path

import pytest

from openrent import cli
from openrent.locking import ScanLock


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
