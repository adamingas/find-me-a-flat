"""DBOS download checkpoints in SQLite, sharing the caller's HTTP event loop."""

import asyncio
import fcntl
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from dbos import DBOS, DBOSConfiguredInstance, SetWorkflowID


def downloads(path, summaries, detail, save_detail, image, save_image):
    """Register configured handlers before launch so persisted work can recover."""

    @DBOS.dbos_class()
    class Downloads(DBOSConfiguredInstance):
        def __init__(self):
            self.path = Path(str(path) + ".downloads.sqlite").resolve()
            self.lock = None
            self.executor = None
            self.running = set()
            self.closing = False
            super().__init__("archive")

        async def __aenter__(self):
            # SQLite supports a local owner, not distributed worker recovery. A
            # second CLI waits, then resumes/skips the first CLI's durable work.
            self.lock = await asyncio.to_thread(Path(str(self.path) + ".lock").open, "a")
            try:
                while True:
                    try:
                        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
            except BaseException:
                self.lock.close()
                raise
            self.executor = asyncio.get_running_loop()._default_executor
            try:
                DBOS(
                    config={
                        "name": "openrent-downloads",
                        "system_database_url": "sqlite:///" + str(self.path),
                        "console_log_level": "ERROR",
                    }
                )
                # Launch in this event loop: DBOS adopts it for queued/recovered
                # coroutines, so HTTPX and the semaphore never cross loops.
                DBOS.launch()
                for queue in ("metadata", "images", "summaries"):
                    await DBOS.register_queue_async(queue, polling_interval_sec=0.01)
                # Finish interrupted summary batches before making new groups.
                previous = await DBOS.list_workflows_async(
                    workflow_id_prefix="batch:",
                    status=["PENDING", "ENQUEUED", "ERROR"],
                )
                for row in previous:
                    handle = await self.existing(row, "summaries")
                    await handle.get_result(polling_interval_sec=0.01)
                return self
            except BaseException:
                await self.__aexit__()
                raise

        async def __aexit__(self, *exc):
            self.closing = True
            tasks = list(self.running)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # SDK async calls install their executor as asyncio's default; its
            # shutdown must not break caller DB/client cleanup or the next scan.
            asyncio.get_running_loop().set_default_executor(self.executor)
            await asyncio.to_thread(DBOS.destroy, destroy_registry=True)
            if self.lock is not None:
                self.lock.close()

        async def perform(self, function, *args):
            if self.closing:
                raise asyncio.CancelledError
            task = asyncio.current_task()
            self.running.add(task)
            try:
                return await function(*args)
            finally:
                self.running.remove(task)

        @DBOS.step()
        async def summary_batch(self, group):
            return await self.perform(summaries, [candidate for candidate, _ in group])

        @DBOS.workflow()
        async def batch(self, group):
            responses = await self.summary_batch(group)
            for candidate, workflow_id in group:
                with SetWorkflowID(workflow_id):
                    await DBOS.enqueue_workflow_async(
                        "metadata",
                        self.metadata,
                        candidate,
                        responses.get(candidate.property.id),
                    )

        @DBOS.step()
        async def read_detail(self, candidate, response):
            return await self.perform(detail, candidate, response)

        @DBOS.step()
        async def save_detail(self, candidate, prop):
            return await self.perform(save_detail, candidate, prop)

        @DBOS.workflow()
        async def metadata(self, candidate, response):
            prop = await self.read_detail(candidate, response)
            return await self.save_detail(candidate, prop)

        @DBOS.step()
        async def read_image(self, url):
            return await self.perform(image, url)

        @DBOS.workflow()
        async def picture(self, url):
            return await self.read_image(url)

        async def existing(self, row, queue):
            if row.status in ("ERROR", "CANCELLED"):
                steps = await DBOS.list_workflow_steps_async(row.workflow_id)
                failed = next((s["function_id"] for s in steps if s.get("error") is not None), None)
                return await DBOS.rewind_workflow_async(
                    row.workflow_id,
                    start_step=failed,
                    queue_name=queue,
                )
            return await DBOS.retrieve_workflow_async(row.workflow_id)

        async def prepare(self, candidates, refresh=False):
            previous = await DBOS.list_workflows_async(
                workflow_id_prefix="property:",
                sort_desc=True,
                load_output=False,
            )
            by_id = {}
            for row in previous:
                by_id.setdefault(int(row.workflow_id.split(":")[1]), row)
            inputs = {candidate.property.id: candidate for candidate in candidates}
            # Persisted unfinished work does not depend on the next search still
            # returning that property. Completed metadata can have missing photos.
            for property_id, row in by_id.items():
                inputs.setdefault(property_id, row.input["args"][0])
            fresh = [
                (candidate, f"property:{property_id}:" + (str(uuid4()) if refresh else "original"))
                for property_id, candidate in inputs.items()
                if property_id not in by_id
                or (refresh and property_id in {c.property.id for c in candidates})
            ]

            async def submit(group):
                digest = sha256("|".join(wid for _, wid in group).encode()).hexdigest()
                with SetWorkflowID("batch:" + digest):
                    handle = await DBOS.enqueue_workflow_async("summaries", self.batch, group)
                await handle.get_result(polling_interval_sec=0.01)

            await asyncio.gather(
                *(submit(fresh[start : start + 20]) for start in range(0, len(fresh), 20))
            )
            handles = [
                (workflow_id, await DBOS.retrieve_workflow_async(workflow_id))
                for _, workflow_id in fresh
            ]
            fresh_ids = {candidate.property.id for candidate, _ in fresh}
            handles.extend(
                [
                    (row.workflow_id, await self.existing(row, "metadata"))
                    for property_id, row in by_id.items()
                    if property_id not in fresh_ids
                ]
            )
            return handles

        async def image(self, metadata_id, property_id, picture):
            digest = sha256(picture.source_url.encode()).hexdigest()
            workflow_id = f"image:{digest}"
            row = await DBOS.get_workflow_status_async(workflow_id)
            if row:
                handle = await self.existing(row, "images")
            else:
                with SetWorkflowID(workflow_id):
                    handle = await DBOS.enqueue_workflow_async(
                        "images",
                        self.picture,
                        picture.source_url,
                    )
            response = await handle.get_result(polling_interval_sec=0.01)
            await save_image(property_id, picture, response)

    return Downloads()
