import asyncio
import sqlite3
import threading

import pytest

from openrent.async_db import AsyncDatabase
from openrent.db import Database
from openrent.models import Image, Property


def listing(property_id=12345):
    return Property(
        id=property_id,
        url=f"https://www.openrent.co.uk/{property_id}",
        title="2 Bed Flat",
        rent_pcm_pence=185000,
        detail_complete=True,
        images=[Image("https://images.openrent.co.uk/example.jpg")],
    )


async def wait_started(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=2)


def assert_no_database_workers():
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("openrent-db")]


def test_worker_io_does_not_block_event_loop_and_connection_keeps_thread_affinity(
    tmp_path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    threads = []

    class SlowDatabase(Database):
        def __init__(self, path):
            threads.append(threading.get_ident())
            super().__init__(path)

        def counts(self):
            threads.append(threading.get_ident())
            started.set()
            assert release.wait(2)
            return super().counts()

        def close(self):
            threads.append(threading.get_ident())
            super().close()

    monkeypatch.setattr("openrent.async_db.Database", SlowDatabase)

    async def run():
        async with AsyncDatabase(tmp_path / "archive.sqlite") as db:
            pending = asyncio.create_task(db.counts())
            try:
                await wait_started(started)
                # This timer must run while the worker is blocked on disk-like IO.
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                assert not pending.done()
                assert threads[0] != threading.get_ident()
            finally:
                release.set()
            assert (await pending)["properties"] == 0

    asyncio.run(run())
    assert len(threads) == 3
    assert len(set(threads)) == 1
    assert_no_database_workers()


def test_context_cancellation_waits_for_submitted_write_then_closes(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite"
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class SlowDatabase(Database):
        def upsert_property(self, property):
            started.set()
            assert release.wait(2)
            return super().upsert_property(property)

        def close(self):
            super().close()
            closed.set()

    monkeypatch.setattr("openrent.async_db.Database", SlowDatabase)

    async def write():
        async with AsyncDatabase(path) as db:
            await db.upsert_property(listing())

    async def run():
        task = asyncio.create_task(write())
        try:
            await wait_started(started)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert not closed.is_set()
            # Repeated cancellation during context cleanup must still drain work.
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert closed.is_set()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM properties").fetchone()[0] == 1
    assert_no_database_workers()


def test_cancelled_queued_call_commits_before_cancelled_close_returns(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite"
    started = threading.Event()
    release = threading.Event()

    class SlowDatabase(Database):
        def upsert_property(self, property):
            if property.id == 1:
                started.set()
                assert release.wait(2)
            return super().upsert_property(property)

    monkeypatch.setattr("openrent.async_db.Database", SlowDatabase)

    async def run():
        async with AsyncDatabase(path) as db:
            first = asyncio.create_task(db.upsert_property(listing(1)))
            closing = None
            try:
                await wait_started(started)
                queued = asyncio.create_task(db.upsert_property(listing(2)))
                await asyncio.sleep(0)
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                closing = asyncio.create_task(db.close())
                await asyncio.sleep(0)
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done()
                with pytest.raises(RuntimeError, match="not open"):
                    await db.counts()
            finally:
                release.set()
            if closing is not None:
                with pytest.raises(asyncio.CancelledError):
                    await closing
            assert await first
            await db.close()

    asyncio.run(run())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM properties ORDER BY id").fetchall() == [(1,), (2,)]
    assert_no_database_workers()


def test_cancellation_while_opening_closes_connection_and_worker(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class SlowOpenDatabase(Database):
        def __init__(self, path):
            started.set()
            assert release.wait(2)
            super().__init__(path)

        def close(self):
            super().close()
            closed.set()

    monkeypatch.setattr("openrent.async_db.Database", SlowOpenDatabase)

    async def open_database():
        async with AsyncDatabase(tmp_path / "archive.sqlite"):
            pytest.fail("Cancelled database opening must not enter the context")

    async def run():
        task = asyncio.create_task(open_database())
        try:
            await wait_started(started)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert closed.is_set()
    assert_no_database_workers()


def test_event_loop_wide_cancellation_still_drains_and_joins_worker(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite"
    started = threading.Event()
    release = threading.Event()

    class SlowDatabase(Database):
        def upsert_property(self, property):
            started.set()
            assert release.wait(2)
            return super().upsert_property(property)

    monkeypatch.setattr("openrent.async_db.Database", SlowDatabase)

    async def run():
        async with AsyncDatabase(path) as db:
            pending = asyncio.create_task(db.upsert_property(listing()))
            closing = None
            try:
                await wait_started(started)
                closing = asyncio.create_task(db.close())
                await asyncio.sleep(0)
                # Simulate asyncio.run's shutdown, including cancellation of
                # cleanup tasks that have not received their first turn yet.
                current = asyncio.current_task()
                for task in asyncio.all_tasks():
                    if task is not current:
                        task.cancel()
                await asyncio.sleep(0.01)
                assert not closing.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            if closing is not None:
                with pytest.raises(asyncio.CancelledError):
                    await closing

    asyncio.run(run())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM properties").fetchone()[0] == 1
    assert_no_database_workers()


def test_initialization_failure_does_not_leave_worker(tmp_path):
    path = tmp_path / "archive.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 999")

    async def run():
        with pytest.raises(ValueError, match="newer than supported"):
            async with AsyncDatabase(path):
                pytest.fail("Unsupported schemas must not enter the context")

    asyncio.run(run())
    assert_no_database_workers()
