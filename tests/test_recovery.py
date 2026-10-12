"""Pipeline crash/restart tests assert application outputs with offline adapters."""

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = Path(__file__).with_name("pipeline_worker.py")


def wait_for(predicate, process, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(f"Worker exited {process.returncode} before the expected boundary")
        time.sleep(0.05)
    raise AssertionError("Worker did not reach the expected boundary")


@pytest.mark.parametrize(
    "phase",
    [
        "after_amenity_save_before_checkpoint",
        "after_images",
        "before_image_checkpoint",
        "after_review_enqueue",
        "before_review_save",
    ],
)
def test_kill_and_restart(tmp_path, phase):
    log = tmp_path / "worker.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(tmp_path), phase], stdout=output, stderr=output
        )
        try:
            wait_for(lambda: (tmp_path / "blocked").exists(), process)
            if phase == "after_review_enqueue":
                # Allow the independently queued review to finish before killing its parent.
                def saved():
                    with sqlite3.connect(tmp_path / "archive.sqlite") as db:
                        return (
                            db.execute("SELECT COUNT(*) FROM property_reviews").fetchone()[0] == 1
                        )

                wait_for(saved, process)
            process.kill()
            process.wait(timeout=5)
            restarted = subprocess.Popen(
                [sys.executable, str(WORKER), str(tmp_path), "normal"], stdout=output, stderr=output
            )
            try:
                wait_for(lambda: (tmp_path / "finished").exists(), restarted)
                assert restarted.wait(timeout=20) == 0
            finally:
                if restarted.poll() is None:
                    restarted.kill()
                    restarted.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    with sqlite3.connect(tmp_path / "archive.sqlite") as db:
        assert db.execute("SELECT count(*) FROM image_blobs").fetchone()[0] == 2
        assert db.execute(
            "SELECT i.source_url, b.content FROM property_images i "
            "JOIN image_blobs b ON b.sha256=i.content_sha256 ORDER BY i.position"
        ).fetchall() == [
            ("https://images.example/0.jpg", b"https://images.example/0.jpg"),
            ("https://images.example/1.jpg", b"https://images.example/1.jpg"),
        ]
        assert db.execute("SELECT decision FROM property_reviews").fetchall() == [("pass",)]
        assert db.execute("SELECT property_id FROM notification_outbox").fetchall() == [(1,)]
        assert db.execute("SELECT count(*) FROM review_images").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if phase == "after_amenity_save_before_checkpoint":
            assert db.execute(
                "SELECT property_id, type FROM amenities ORDER BY type"
            ).fetchall() == [(1, "grocer"), (1, "gym")]
            assert (tmp_path / "gym-calls").read_text().splitlines() == ["1"]


def test_daemon_shutdown_during_image_download(tmp_path):
    import signal

    log = tmp_path / "worker.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(tmp_path), "daemon_shutdown"],
            stdout=output,
            stderr=output,
        )
        try:
            wait_for(lambda: (tmp_path / "blocked").exists(), process)
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=35) == 0, log.read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert (tmp_path / "stopped").exists()
    assert "Traceback" not in log.read_text(), log.read_text()
