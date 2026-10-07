"""Durable stage results in a sidecar; OS locks release abandoned property claims."""

import asyncio
import hashlib
import pickle
import sqlite3
from pathlib import Path

from .async_db import AsyncDatabase
from .locking import ScanLock


class JobDatabase:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, input BLOB, result BLOB)"
        )
        self.connection.commit()

    def close(self):
        self.connection.close()

    def get(self, key):
        row = self.connection.execute("SELECT result FROM jobs WHERE key = ?", (key,)).fetchone()
        return pickle.loads(row[0]) if row and row[0] is not None else None

    def put(self, key, result):
        with self.connection:
            self.connection.execute(
                "INSERT INTO jobs(key, result) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET result = excluded.result",
                (key, pickle.dumps(result)),
            )

    def imported(self, property_id, images):
        with self.connection:
            self.connection.executemany(
                "INSERT INTO jobs(key, input) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET input = excluded.input",
                [(f"image:{property_id}:{i.source_url}", pickle.dumps(i)) for i in images],
            )
            self.connection.execute(
                "INSERT INTO jobs(key, result) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET result = excluded.result",
                (f"import:{property_id}", pickle.dumps(True)),
            )

    def images(self, property_id):
        return [
            pickle.loads(row[0])
            for row in self.connection.execute(
                "SELECT input FROM jobs WHERE key LIKE ? AND input IS NOT NULL AND result IS NULL",
                (f"image:{property_id}:%",),
            )
        ]

    def enqueue(self, context):
        property_id = context[0].property.id
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO jobs(key, input) VALUES (?, ?)",
                (f"import:{property_id}", pickle.dumps(context)),
            )

    def work(self):
        return [
            pickle.loads(row[0])
            for row in self.connection.execute(
                "SELECT j.input FROM jobs j WHERE j.key LIKE 'import:%' AND j.input IS NOT NULL "
                "AND (j.result IS NULL OR EXISTS (SELECT 1 FROM jobs i "
                "WHERE i.key LIKE 'image:' || substr(j.key, 8) || ':%' AND i.result IS NULL))"
            )
        ]

    def discard_images(self, property_id):
        with self.connection:
            self.connection.execute(
                "DELETE FROM jobs WHERE key LIKE ?", (f"image:{property_id}:%",)
            )

    def refresh(self, property_id):
        with self.connection:
            self.connection.executemany(
                "DELETE FROM jobs WHERE key = ?",
                [(f"{stage}:{property_id}",) for stage in ("summary", "detail")],
            )
            self.connection.execute(
                "UPDATE jobs SET result = NULL WHERE key = ?", (f"import:{property_id}",)
            )
            # Remove obsolete gallery jobs; downloaded bytes remain in the listing DB.
            self.connection.execute(
                "DELETE FROM jobs WHERE key LIKE ?", (f"image:{property_id}:%",)
            )


class DownloadJobs(AsyncDatabase):
    def __init__(self, database):
        self.path = Path(str(database.resolve()) + ".downloads.sqlite")
        super().__init__(self.path)
        self._claims = set()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._database = JobDatabase(self.path)

    def claim(self, property_id):
        if property_id in self._claims:
            return None
        lock = ScanLock(Path(str(self.path) + ".claims") / str(property_id))
        try:
            lock.__enter__()
        except ValueError:
            return None
        self._claims.add(property_id)
        return lock

    def release(self, property_id, lock):
        self._claims.discard(property_id)
        lock.__exit__()

    async def get(self, key):
        return await self._call("get", key)

    async def put(self, key, result):
        await self._call("put", key, result)

    async def imported(self, property_id, images):
        await self._call("imported", property_id, images)

    async def images(self, property_id):
        return await self._call("images", property_id)

    async def refresh(self, property_id):
        await self._call("refresh", property_id)

    async def enqueue(self, context):
        await self._call("enqueue", context)

    async def work(self):
        return await self._call("work")

    async def discard_images(self, property_id):
        await self._call("discard_images", property_id)

    async def claim_image(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        while (lock := self.claim(key)) is None:
            await asyncio.sleep(0.05)
        return key, lock
