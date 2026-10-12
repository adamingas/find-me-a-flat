"""The shared connection keeps the event loop responsive and rolls back cancellation."""

import asyncio
import threading

import pytest
from archive_helpers import listing

from openrent import db as database


@pytest.mark.asyncio
async def test_slow_sql_does_not_block_the_event_loop():
    started, release = threading.Event(), threading.Event()

    def slow_query():
        started.set()
        assert release.wait(2)
        return 1

    async with database.connect(":memory:") as db:
        await db.create_function("slow_query", 0, slow_query)
        pending = asyncio.create_task(database.query(db, "SELECT slow_query() AS result"))
        try:
            async with asyncio.timeout(1):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            assert not pending.done()
        finally:
            release.set()
        assert await pending == [{"result": 1}]


@pytest.mark.asyncio
async def test_cancelled_update_rolls_back_and_releases_the_connection(monkeypatch):
    inserted = asyncio.Event()
    original_sync = database._sync_children

    async def interrupted(*args):
        inserted.set()
        await asyncio.Future()

    async with database.connect(":memory:") as db:
        monkeypatch.setattr(database, "_sync_children", interrupted)
        pending = asyncio.create_task(database.upsert_property(db, listing()))
        await asyncio.wait_for(inserted.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await database.get_property(db, 1) is None
        monkeypatch.setattr(database, "_sync_children", original_sync)
        assert await database.upsert_property(db, listing())
        assert await database.get_property(db, 1) is not None
