"""Scheduled discovery and resumable downloads using shared HTTP/SQLite resources."""

import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .client import BASE_URL, FetchError
from .models import Candidate, SearchData
from .parsing import enrich_summary, parse_property, parse_search
from .search import SearchError, matches_criteria


def log(args, message):
    if not args.quiet:
        print(message, file=sys.stderr, flush=True)


async def gather_tasks(coroutines):
    """Join tasks before closing shared resources, including on failure or cancellation."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def discover(args, client, jobs, api, website):
    log(args, f"Searching {api.location} ...")
    response = await client.search(api.parameters())
    reference_date = datetime.now(ZoneInfo("Europe/London")).date()
    search = await asyncio.to_thread(
        parse_search, response.text, str(response.url), reference_date=reference_date
    )
    if api.radius_minutes is not None and "minute" not in (search.distance_unit or "").lower():
        raise SearchError(
            "OpenRent returned distance results for this commute request. "
            "Its travel-time search supports London locations; use --radius-distance elsewhere."
        )
    candidates = [
        candidate
        for candidate in search.candidates
        if matches_criteria(api, website, candidate, search, reference_date=reference_date)
    ]
    log(args, f"{len(search.candidates)} candidates; {len(candidates)} match; queuing matches.")
    context = SearchData([], latitude=search.latitude, longitude=search.longitude)
    for candidate in candidates:
        await jobs.enqueue((candidate, api, website, context, reference_date, args.post_filter))


async def drain(args, client, db, jobs):
    new_properties = downloaded = errors = imported = filtered_out = 0
    candidates = await jobs.work()
    if not candidates:
        return 0

    async def download(property_id, picture):
        nonlocal downloaded, errors
        if await db.image_downloaded(property_id, picture.source_url):
            await jobs.put(f"image:{property_id}:{picture.source_url}", True)
            return
        key, lock = await jobs.claim_image(picture.source_url)
        try:
            try:
                response = await jobs.get(f"response:{picture.source_url}")
                if response is None:
                    response = await client.image(picture.source_url)
                    await jobs.put(f"response:{picture.source_url}", response)
            except FetchError as exc:
                errors += 1
                await db.record_image_error(property_id, picture.source_url, str(exc))
                log(args, f"{property_id} image: {exc}")
                return
            content, mime, dimensions, headers = response
            picture.width, picture.height = dimensions
            stored = await db.store_image(
                property_id,
                picture,
                content,
                mime,
                etag=headers.get("etag"),
                last_modified=headers.get("last-modified"),
            )
            downloaded += stored
            await jobs.put(f"image:{property_id}:{picture.source_url}", True)
        finally:
            jobs.release(key, lock)

    async def load(item):
        candidate, saved_api, saved_website, saved_search, admitted_date, *settings = item
        post_filter = settings[0] if settings else getattr(args, "post_filter", False)
        nonlocal new_properties, imported, errors, filtered_out
        property_id = candidate.property.id
        if not await jobs.get(f"import:{property_id}"):
            try:
                summary = await jobs.get(f"summary:{property_id}")
                if summary is not None:
                    try:
                        enrich_summary(candidate, summary)
                    except ValueError as exc:
                        log(
                            args,
                            f"{property_id}: invalid summary; using detail page: {exc}",
                        )
                prop = await jobs.get(f"detail:{property_id}")
                if prop is None:
                    detail = await client.get(BASE_URL + f"/{property_id}")
                    prop = await asyncio.to_thread(
                        parse_property, detail.text, str(detail.url), candidate
                    )
                    await jobs.put(f"detail:{property_id}", prop)
                current = Candidate(prop, candidate.distance_km, candidate.commute_minutes)
                matches = matches_criteria(
                    saved_api,
                    saved_website,
                    current,
                    saved_search,
                    reference_date=admitted_date,
                )
            except (FetchError, ValueError) as exc:
                errors += 1
                log(args, f"{property_id}: {exc}")
                return
            if not matches:
                if await db.get_property(property_id):
                    await db.upsert_property(prop)
                await jobs.imported(property_id, [])
                await jobs.put(f"ready:{property_id}", True)
                log(args, f"{property_id}: does not match its saved filters.")
                return
            inserted = await db.upsert_property(prop)
            new_properties += inserted
            # The output is committed before completing import and publishing image jobs.
            await jobs.imported(property_id, prop.images)
            imported += 1
            log(args, f"[{imported}/{len(candidates)}] {property_id}: {prop.title}")
        if await db.get_property(property_id):
            if post_filter and not await db.filter_properties([property_id]):
                filtered_out += 1
                await jobs.discard_images(property_id)
                await jobs.put(f"ready:{property_id}", True)
                return
            await gather_tasks(
                download(property_id, picture) for picture in await jobs.images(property_id)
            )
            if await jobs.images(property_id):
                return
            await db.publish_ready(property_id)
        await jobs.put(f"ready:{property_id}", True)

    async def load_group(group):
        claims = []
        try:
            for item in group:
                candidate = item[0]
                lock = jobs.claim(candidate.property.id)
                if lock is not None:
                    claims.append((item, lock))
                    if not await jobs.get(f"import:{candidate.property.id}"):
                        saved = await db.get_property(candidate.property.id)
                        if saved is not None and saved["source_html"]:
                            await jobs.imported(
                                candidate.property.id,
                                await db.get_images(candidate.property.id),
                            )
            missing = [
                item[0].property.id
                for item, _ in claims
                if not await jobs.get(f"import:{item[0].property.id}")
                and await jobs.get(f"summary:{item[0].property.id}") is None
            ]
            if missing:
                try:
                    summaries = await client.summaries(missing)
                    for summary in summaries:
                        await jobs.put(f"summary:{int(summary['id'])}", summary)
                except (FetchError, ValueError, TypeError) as exc:
                    log(args, f"Summary API unavailable; fetching detail pages: {exc}")
            await gather_tasks(load(item) for item, _ in claims)
        finally:
            for item, lock in claims:
                jobs.release(item[0].property.id, lock)

    await gather_tasks(
        load_group(candidates[start : start + 20]) for start in range(0, len(candidates), 20)
    )
    print(
        f"Imported {imported} properties ({new_properties} new); downloaded {downloaded} images; "
        f"deleted {filtered_out}; {errors} errors. Database: {args.db.resolve()}",
        flush=True,
    )
    return 1 if errors else 0
