"""Offline subprocess used to kill the real pipeline at durable boundaries."""

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import click
from dbos import DBOS, SetWorkflowID
from pydantic import BaseModel

from openrent import db as database
from openrent import ingestion, review
from openrent.daemon import register_queues
from openrent.models import Candidate, Image, Property, SearchData
from openrent.review import ReviewConfig
from openrent.review_models import JudgementOutput
from openrent.runtime import Runtime, configure
from openrent.search import ApiFilters, WebsiteFilters


def property_fixture(property_id=1):
    return Property(
        property_id,
        f"https://www.openrent.co.uk/{property_id}",
        title="Test flat",
        is_live=True,
        is_shared=False,
        is_studio=False,
        bedrooms=2,
        detail_complete=True,
        images=[Image(f"https://images.example/{index}.jpg", position=index) for index in range(2)],
    )


class GymData(BaseModel):
    gyms: list[dict]


class FakeGymMetadataFetcher:
    type = "gym"
    model = GymData

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self, property):
        return {"gyms": [{"name": "Leisure Centre", "walking_seconds": 284}]}


class GrocerData(BaseModel):
    grocers: list[dict]


class FakeGrocerMetadataFetcher:
    type = "grocer"
    model = GrocerData

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self, property):
        return {"grocers": [{"name": "Supermarket", "walking_seconds": 300}]}


def judgement(outcome=True):
    fields = {
        name: {"outcome": outcome, "evidence": "Offline fixture evidence."}
        for name in JudgementOutput.model_fields
        if name not in {"area_m2", "floor", "summary"}
    }
    return JudgementOutput(**fields, area_m2=None, floor=None, summary="Offline assessment.")


async def run(directory, phase):
    async def block():
        (directory / "blocked").write_text(phase)
        await asyncio.Event().wait()

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, parameters):
            return SimpleNamespace(text="offline", url="https://example.test/search")

        async def summaries(self, ids):
            return []

        async def get(self, url):
            return SimpleNamespace(text="offline detail", url=url)

        async def image(self, url):
            if phase == "daemon_shutdown":
                await block()
            return url.encode(), "image/jpeg", (10, 10), {}

    async def judge(evidence, *args, **kwargs):
        assert len(evidence.images) == 2
        assert all(image.content for image in evidence.images)
        return judgement()

    ingestion.parse_property = lambda *args: property_fixture()
    review.judge_property = judge

    class RecordedGymMetadataFetcher(FakeGymMetadataFetcher):
        async def fetch(self, property):
            with (directory / "gym-calls").open("a") as output:
                output.write(f"{property.id}\n")
            return await super().fetch(property)

    ingestion.GymMetadataFetcher = RecordedGymMetadataFetcher
    ingestion.GrocerMetadataFetcher = FakeGrocerMetadataFetcher
    if phase == "daemon_shutdown":
        from openrent import daemon

        daemon.OpenRentClient = FakeClient
        ingestion.parse_search = lambda *args, **kwargs: SearchData([Candidate(property_fixture())])
        args = SimpleNamespace(
            db=directory / "archive.sqlite",
            timezone="Europe/London",
            cron="0 1 1 1 *",
            notify_cron_schedule=None,
            email_to=(),
            email_preview=None,
            criteria_file=None,
            post_filter=False,
            timeout=10,
            concurrency=2,
            requests_per_minute=60,
            cookie_file=None,
            quiet=True,
            review_concurrency=1,
            run_now=True,
        )
        await daemon.run_daemon(args, ApiFilters(), WebsiteFilters())
        (directory / "stopped").write_text("ok")
        return
    archive = directory / "archive.sqlite"
    async with database.connect(archive) as db:
        original_store = database.store_image
        original_save = database.save_review
        original_store_amenity = database.store_amenity
        original_enqueue = DBOS.enqueue_workflow_async

        async def store(*args, **kwargs):
            await original_store(*args, **kwargs)
            if phase == "before_image_checkpoint":
                await block()

        async def save(*args, **kwargs):
            if phase == "before_review_save":
                await block()
            return await original_save(*args, **kwargs)

        async def store_amenity(*args, **kwargs):
            result = await original_store_amenity(*args, **kwargs)
            if phase == "after_amenity_save_before_checkpoint":
                await block()
            return result

        async def enqueue(queue, fn, *args, **kwargs):
            if queue == ingestion.REVIEW_QUEUE and phase == "after_images":
                await block()
            handle = await original_enqueue(queue, fn, *args, **kwargs)
            if queue == ingestion.REVIEW_QUEUE and phase == "after_review_enqueue":
                await block()
            return handle

        database.store_image, database.save_review = store, save
        database.store_amenity = store_amenity
        DBOS.enqueue_workflow_async = enqueue
        configure(Runtime(FakeClient(), db, quiet=True))
        DBOS(
            config={
                "name": "openrent-test",
                "system_database_url": f"sqlite:///{directory / 'system.sqlite'}",
                "log_level": "ERROR",
            }
        )
        DBOS.launch()
        await register_queues(2, 1)
        config = ingestion.DiscoveryConfig(
            ApiFilters(),
            WebsiteFilters(),
            review=ReviewConfig("Preferences", "a" * 64, "offline", 30),
        )
        try:
            with SetWorkflowID("property:1"):
                handle = await DBOS.enqueue_workflow_async(
                    ingestion.PROPERTY_QUEUE,
                    ingestion.download_property,
                    Candidate(property_fixture()),
                    config,
                    SearchData([]),
                    date(2026, 10, 10),
                )
            await handle.get_result()
            review_handle = await DBOS.retrieve_workflow_async("review:1")
            await review_handle.get_result()
            (directory / "finished").write_text("ok")
        finally:
            await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=1)
            configure(None)


@click.command()
@click.argument("directory", type=click.Path(path_type=Path, exists=True))
@click.argument("phase")
def main(directory, phase):
    asyncio.run(run(directory, phase))


if __name__ == "__main__":
    main()
