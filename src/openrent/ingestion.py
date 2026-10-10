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
    if args.sort == "rent-asc":
        candidates.sort(key=lambda c: c.property.rent_pcm_pence or 0)
    elif args.sort == "rent-desc":
        candidates.sort(key=lambda c: c.property.rent_pcm_pence or 0, reverse=True)
    elif args.sort == "newest":
        candidates.sort(key=lambda c: c.property.first_listed_at or "", reverse=True)
    else:
        candidates.sort(
            key=lambda c: c.commute_minutes if api.radius_minutes is not None else c.distance_km
        )
    log(
        args,
        f"{len(search.candidates)} candidates; {len(candidates)} match; importing all matches.",
    )
    if args.dry_run:
        print(f"{len(candidates)} matches; database unchanged.")
        for candidate in candidates:
            prop = candidate.property
            radius = (
                f"{candidate.commute_minutes:g} min"
                if api.radius_minutes is not None
                else f"{candidate.distance_km:.2f} km"
            )
            print(
                f"{prop.id}\t£{prop.rent_pcm_pence / 100:.2f}/month\t{radius}\t{BASE_URL}/{prop.id}"
            )
        return {}

    context = SearchData([], latitude=search.latitude, longitude=search.longitude)
    fresh = {c.property.id: (c, api, website, context, reference_date) for c in candidates}
    if jobs is not None:
        for item in fresh.values():
            await jobs.enqueue(item, refresh=args.refresh)
    return fresh


async def drain(args, client, db, jobs, fresh=None):
    new_properties = downloaded = errors = imported = filtered_out = 0
    seen = []
    work = {item[0].property.id: item for item in await jobs.work()}
    # Manual fetches also revisit completed matches for a changed post-filter.
    if args.refresh:
        work.update(fresh or {})
    else:
        for property_id, item in (fresh or {}).items():
            work.setdefault(property_id, item)
    candidates = list(work.values())
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
        candidate, saved_api, saved_website, saved_search, admitted_date = item
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
                if args.no_source_html:
                    prop.source_html = None
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
                log(args, f"{property_id}: no longer matches after refreshing details.")
                return
            inserted = await db.upsert_property(prop)
            new_properties += inserted
            # The output is committed before completing import and publishing image jobs.
            await jobs.imported(property_id, prop.images)
            imported += 1
            log(args, f"[{imported}/{len(candidates)}] {property_id}: {prop.title}")
        if await db.get_property(property_id):
            seen.append(property_id)
            if args.post_filter and not await db.filter_properties([property_id]):
                filtered_out += 1
                await jobs.discard_images(property_id)
                return
            if not args.skip_images:
                await gather_tasks(
                    download(property_id, picture) for picture in await jobs.images(property_id)
                )

    async def load_group(group):
        claims = []
        try:
            for item in group:
                candidate = item[0]
                lock = jobs.claim(candidate.property.id)
                if lock is not None:
                    claims.append((item, lock))
                    if await jobs.get(f"refresh:{candidate.property.id}"):
                        await jobs.refresh(candidate.property.id)
                    elif not await jobs.get(f"import:{candidate.property.id}"):
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
    if args.post_filter:
        log(
            args,
            f"Post-filter kept {len(seen) - filtered_out} listings; deleted {filtered_out}.",
        )
    counts = await db.counts()
    filter_summary = f"Post-filter deleted {filtered_out}. " if args.post_filter else ""
    print(
        f"Imported {imported} properties ({new_properties} new); downloaded {downloaded} images; "
        f"{errors} errors. {filter_summary}Database: {args.db.resolve()} "
        f"({counts['properties']} properties, {counts['downloaded_images']} stored images).",
        flush=True,
    )
    return 1 if errors else 0
