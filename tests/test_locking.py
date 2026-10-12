"""Process locks protect daemon ownership."""

from pathlib import Path

import pytest

from openrent.locking import ScanLock


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
