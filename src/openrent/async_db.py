"""Async access to SQLite through one dedicated connection-owning worker."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar

from .db import Database
from .models import Image, Property

T = TypeVar("T")


class AsyncDatabase:
    """Keep SQLite and image BLOB writes off the network event loop.

    The connection is created, used, and closed in the same single worker;
    SQLite's normal thread affinity remains enabled. Operations are submitted
    in order and commit using the synchronous database's existing transactions.
    Cancelling an await does not cancel an already submitted write. Closing
    drains that work before closing the connection and joining the worker.
    """

    def __init__(self, path: str | Path):
        self._path = path
        self._executor: ThreadPoolExecutor | None = None
        self._database: Database | None = None
        self._state = "new"
        self._close_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._state != "new":
            raise RuntimeError("AsyncDatabase cannot be entered more than once")
        self._state = "opening"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openrent-db")
        try:
            await self._submit(self._open)
        except BaseException:
            # Initialization itself can be cancelled while the worker is still
            # opening SQLite. Its queued close will run after initialization.
            await self.close()
            raise
        if self._state != "opening":
            await self.close()
            raise RuntimeError("AsyncDatabase was closed while opening")
        self._state = "open"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def _open(self) -> None:
        self._database = Database(self._path)

    @staticmethod
    def _observe_result(future: asyncio.Future[Any]) -> None:
        # A cancelled caller can no longer retrieve a worker exception. Observe
        # it here to avoid an unhandled-future warning; active awaits still raise.
        if not future.cancelled():
            future.exception()

    def _queue(self, operation: Callable[[], T]) -> asyncio.Future[T]:
        assert self._executor is not None
        future = asyncio.wrap_future(self._executor.submit(operation))
        future.add_done_callback(self._observe_result)
        return future

    async def _submit(self, operation: Callable[[], T]) -> T:
        return await asyncio.shield(self._queue(operation))

    @staticmethod
    async def _finish_cleanup(future: asyncio.Future[T]) -> tuple[T, bool]:
        """Drain a worker future even if event-loop shutdown cancels this task."""
        cancelled = False
        while True:
            try:
                return await asyncio.shield(future), cancelled
            except asyncio.CancelledError:
                cancelled = True
                if future.cancelled():
                    raise

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._state != "open":
            raise RuntimeError("AsyncDatabase is not open")

        def operation() -> Any:
            assert self._database is not None
            return getattr(self._database, method)(*args, **kwargs)

        return await self._submit(operation)

    async def _close(self) -> None:
        def close_connection() -> None:
            if self._database is not None:
                try:
                    self._database.close()
                finally:
                    self._database = None

        assert self._executor is not None
        cancelled = False
        try:
            _, cancelled = await self._finish_cleanup(self._queue(close_connection))
        finally:
            # The connection close is queued behind all submitted operations.
            # Join outside the event loop even when opening/closing failed.
            shutdown = asyncio.get_running_loop().run_in_executor(
                None, partial(self._executor.shutdown, wait=True)
            )
            try:
                _, interrupted = await self._finish_cleanup(shutdown)
                cancelled |= interrupted
            finally:
                self._state = "closed"
        if cancelled:
            raise asyncio.CancelledError

    async def close(self) -> None:
        """Finish queued work and release the worker, even during cancellation."""
        if self._state in {"new", "closed"}:
            self._state = "closed"
            return
        if self._close_task is None:
            self._state = "closing"
            self._close_task = asyncio.create_task(self._close())

        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                # A second cancellation during __aexit__ must not abandon the
                # connection or its worker. Propagate it after cleanup finishes.
                cancelled = True
                if self._close_task.cancelled():
                    if self._state == "closed":
                        break
                    # asyncio.run cancels every pending task at shutdown. If
                    # our cleanup task was cancelled before its first turn,
                    # restart it so the owning worker still gets drained.
                    self._close_task = asyncio.create_task(self._close())
        if cancelled:
            raise asyncio.CancelledError

    async def get_property(self, property_id: int) -> sqlite3.Row | None:
        return await self._call("get_property", property_id)

    async def get_images(self, property_id: int) -> list[Image]:
        return await self._call("get_images", property_id)

    async def upsert_property(self, property: Property) -> bool:
        return await self._call("upsert_property", property)

    async def filter_properties(self, property_ids: list[int]) -> list[int]:
        return await self._call("filter_properties", property_ids)

    async def delete_properties(self, property_ids: list[int]) -> int:
        return await self._call("delete_properties", property_ids)

    async def image_downloaded(self, property_id: int, url: str) -> bool:
        return await self._call("image_downloaded", property_id, url)

    async def store_image(
        self,
        property_id: int,
        image: Image,
        content: bytes,
        content_type: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        return await self._call(
            "store_image",
            property_id,
            image,
            content,
            content_type,
            etag=etag,
            last_modified=last_modified,
        )

    async def record_image_error(self, property_id: int, url: str, message: str) -> None:
        await self._call("record_image_error", property_id, url, message)

    async def counts(self) -> dict[str, int]:
        return await self._call("counts")
