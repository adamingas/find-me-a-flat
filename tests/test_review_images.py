import asyncio
import io
from contextlib import asynccontextmanager

import httpx
import pytest
from PIL import Image as PILImage

from openrent.client import OpenRentClient
from openrent.models import Image, Property
from openrent.review_db import AsyncReviewDatabase, ReviewDatabase
from openrent.review_images import prepare_gallery


def png_bytes(color):
    buffer = io.BytesIO()
    PILImage.new("RGB", (7, 9), color).save(buffer, format="PNG")
    return buffer.getvalue()


def gallery_listing():
    return Property(
        id=1,
        url="https://www.openrent.co.uk/1",
        title="Archived flat",
        description="Original listing facts remain unchanged.",
        is_live=True,
        detail_complete=True,
        images=[
            Image("https://images.openrent.co.uk/living.png", position=0, caption="Living room"),
            Image("https://images.openrent.co.uk/bedroom.png", position=1, caption="Bedroom"),
            Image(
                "https://images.openrent.co.uk/floorplan.png",
                position=2,
                kind="floorplan",
                caption="Floor plan",
            ),
            Image("https://images.openrent.co.uk/map.png", position=3, kind="map", caption="Map"),
        ],
    )


@asynccontextmanager
async def with_transport(handler, *, concurrency=2):
    client = OpenRentClient(concurrency=concurrency, requests_per_second=1_000_000)
    headers, hooks = client.http.headers, client.http.event_hooks
    await client.http.aclose()
    client.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=headers,
        event_hooks=hooks,
        follow_redirects=True,
    )
    async with client:
        yield client


def test_preparation_downloads_every_kind_preserves_bytes_and_skips_complete_gallery(tmp_path):
    path = tmp_path / "archive.sqlite"
    listing = gallery_listing()
    contents = {
        image.source_url: png_bytes(color)
        for image, color in zip(listing.images, ("red", "green", "blue", "yellow"), strict=True)
    }
    requests = []
    active = maximum = 0
    reports = []

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        requests.append(str(request.url))
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                content=contents[str(request.url)],
                headers={
                    "ETag": "source-version",
                    "Last-Modified": "Wed, 01 Oct 2025 12:00:00 GMT",
                },
            )
        finally:
            active -= 1

    async def scenario():
        async with AsyncReviewDatabase(path) as db, with_transport(handler) as client:
            await db.upsert_property(listing)
            await db.store_image(
                1, listing.images[0], contents[listing.images[0].source_url], "image/png"
            )
            assert await prepare_gallery(db, 1, client=client, report=reports.append)
            assert not await db.pending_review_images(1)
            counts = await db.counts()
            assert counts["image_blobs"] == 4 and counts["property_images"] == 4
            assert await prepare_gallery(db, 1, client=client, report=reports.append)
            assert await db.counts() == counts
            assert (await db.get_property(1))["description"] == listing.description

    asyncio.run(scenario())
    assert requests == [image.source_url for image in listing.images[1:]]
    assert maximum == 2
    assert reports == ["1: gallery downloaded 3 missing images."]
    with ReviewDatabase(path) as db:
        rows = db.connection.execute(
            "SELECT i.*, b.content, b.content_type FROM property_images i JOIN image_blobs b "
            "ON b.sha256=i.content_sha256 ORDER BY i.position"
        ).fetchall()
        for row, image in zip(rows, listing.images, strict=True):
            assert row["content"] == contents[image.source_url]
            assert row["kind"] == image.kind and row["caption"] == image.caption
            assert row["content_type"] == "image/png"
            if image.position:
                assert (row["width"], row["height"]) == (7, 9)
                assert row["etag"] == "source-version"
                assert row["last_modified"] == "Wed, 01 Oct 2025 12:00:00 GMT"


def test_invalid_image_remains_unprocessed_and_next_preparation_retries_only_that_url(tmp_path):
    path = tmp_path / "archive.sqlite"
    listing = gallery_listing()
    listing.images = listing.images[:2]
    requests = []
    good_content = png_bytes("red")

    async def handler(request):
        requests.append(str(request.url))
        if str(request.url) == listing.images[1].source_url and len(requests) == 2:
            return httpx.Response(200, text="<html>This is not an image.</html>")
        return httpx.Response(200, content=good_content)

    async def scenario():
        async with AsyncReviewDatabase(path) as db, with_transport(handler) as client:
            await db.upsert_property(listing)
            assert not await prepare_gallery(db, 1, client=client, report=lambda _: None)
            remaining = await db.pending_review_images(1)
            assert len(remaining) == 1 and remaining[0]["download_status"] == "error"
            assert "invalid image" in remaining[0]["last_error"]
            assert await db.unprocessed_ids() == [1]
            assert await prepare_gallery(db, 1, client=client, report=lambda _: None)
            assert not await db.pending_review_images(1)
            assert await db.unprocessed_ids() == [1]  # Downloading is not reviewing.
            assert not await prepare_gallery(db, 999, client=client, report=lambda _: None)

    asyncio.run(scenario())
    assert requests == [
        listing.images[0].source_url,
        listing.images[1].source_url,
        listing.images[1].source_url,
    ]


@pytest.mark.parametrize("removal_phase", ["during_download", "before_store"])
def test_preparation_does_not_restore_removed_gallery_urls(tmp_path, monkeypatch, removal_phase):
    path = tmp_path / "archive.sqlite"
    listing = gallery_listing()
    listing.images = listing.images[:2]
    reports = []

    async def scenario():
        async with AsyncReviewDatabase(path) as db:
            await db.upsert_property(listing)
            await db.store_image(1, listing.images[0], png_bytes("red"), "image/png")

            async def remove_association():
                replacement = gallery_listing()
                replacement.images = replacement.images[:1]
                await db.upsert_property(replacement)

            if removal_phase == "before_store":
                original_store = db.store_image

                async def remove_before_store(*args, **kwargs):
                    assert kwargs["require_association"] is True
                    await remove_association()
                    return await original_store(*args, **kwargs)

                monkeypatch.setattr(db, "store_image", remove_before_store)

            async def handler(request):
                assert str(request.url) == listing.images[1].source_url
                if removal_phase == "during_download":
                    await remove_association()
                return httpx.Response(200, content=png_bytes("blue"))

            async with with_transport(handler) as client:
                assert await prepare_gallery(db, 1, client=client, report=reports.append)
            counts = await db.counts()
            assert counts["property_images"] == counts["image_blobs"] == 1
            assert not await db.pending_review_images(1)

    asyncio.run(scenario())
    assert reports == ["1: gallery downloaded 0 missing images."]
