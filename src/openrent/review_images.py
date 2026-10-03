"""Finish archived galleries before a model claims their review snapshot."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .client import FetchError, OpenRentClient
from .models import Image
from .review_db import AsyncReviewDatabase


async def prepare_gallery(
    db: AsyncReviewDatabase,
    property_id: int,
    *,
    client: OpenRentClient,
    report: Callable[[str], object] = print,
) -> bool:
    """Download every missing archived gallery image, without refetching listing facts.

    The shared OpenRent client owns the HTTP concurrency/rate limits and image
    validation. A failed download remains retryable and prevents a complete
    review. The subsequent snapshot claim also rejects empty/no-photo galleries.
    """
    property_row = await db.get_property(property_id)
    if property_row is None or property_row["is_live"] == 0:
        return False
    pending = await db.pending_review_images(property_id)
    if not pending:
        return True

    async def current_image(url: str) -> dict | None:
        return next(
            (
                row
                for row in await db.pending_review_images(property_id)
                if row["source_url"] == url
            ),
            None,
        )

    async def download(row: dict) -> str:
        url = row["source_url"]
        # The pending query also detects a downloaded association with a missing
        # BLOB; image_downloaded alone cannot distinguish that damaged archive.
        if await current_image(url) is None:
            return "skipped"
        try:
            content, mime, dimensions, headers = await client.image(url)
        except FetchError as exc:
            # An importer can replace the gallery while this request is in flight.
            # Never recreate a removed association just to attach an error.
            if await current_image(url) is not None:
                await db.record_image_error(property_id, url, str(exc))
                return "error"
            return "skipped"

        current = await current_image(url)
        if current is None:
            return "skipped"
        picture = Image(
            source_url=url,
            position=current["position"],
            kind=current["kind"],
            caption=current["caption"],
            width=dimensions[0],
            height=dimensions[1],
        )
        stored = await db.store_image(
            property_id,
            picture,
            content,
            mime,
            etag=headers.get("etag"),
            last_modified=headers.get("last-modified"),
            require_association=True,
        )
        return "downloaded" if stored else "skipped"

    tasks = [asyncio.create_task(download(row)) for row in pending]
    try:
        outcomes = await asyncio.gather(*tasks)
    except BaseException:
        # Do not leave downloads using the shared client after their owner exits.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    remaining = await db.pending_review_images(property_id)
    property_row = await db.get_property(property_id)
    ready = not remaining and property_row is not None and property_row["is_live"] != 0
    message = f"{property_id}: gallery downloaded {outcomes.count('downloaded')} missing images"
    if not ready:
        message += f"; {len(remaining)} remain unavailable"
    report(message + ".")
    return ready
