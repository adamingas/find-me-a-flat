"""Discovery, property downloads, and the durable handoff to AI review."""

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dbos import DBOS, SetWorkflowID

from . import db as database
from .amenities import MetadataFetcher, fetch_and_store_amenities
from .client import BASE_URL, FetchError
from .commute import CommuteMetadataFetcher
from .filtering import matches_post_filter, post_amenities_filter
from .grocers import GrocerMetadataFetcher
from .gyms import GymMetadataFetcher
from .models import Candidate, Image, Property, SearchData
from .parsing import enrich_summary, parse_property, parse_search
from .review import ReviewConfig, review_property
from .runtime import resources
from .search import ApiFilters, SearchError, WebsiteFilters, matches_criteria

PROPERTY_QUEUE = "properties"
REVIEW_QUEUE = "reviews"
DISCOVERY_QUEUE = "discovery"
NOTIFY_QUEUE = "notifications"


@dataclass(frozen=True)
class DiscoveryConfig:
    api: ApiFilters
    website: WebsiteFilters
    post_filter: bool = False
    review: ReviewConfig | None = None


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def search_properties(config: DiscoveryConfig, reference_date: date) -> SearchData:
    runtime = resources()
    runtime.report(f"Searching {config.api.location} ...")
    response = await runtime.client.search(config.api.parameters())
    search = await asyncio.to_thread(
        parse_search, response.text, str(response.url), reference_date=reference_date
    )
    if (
        config.api.radius_minutes is not None
        and "minute" not in (search.distance_unit or "").lower()
    ):
        raise SearchError(
            "OpenRent did not return commute results; use --radius-distance outside London."
        )
    return search


@DBOS.workflow()
async def discover(scheduled_time: datetime, config: DiscoveryConfig) -> None:
    reference_date = scheduled_time.astimezone(ZoneInfo("Europe/London")).date()
    search = await search_properties(config, reference_date)
    context = SearchData(
        [],
        latitude=search.latitude,
        longitude=search.longitude,
        location=search.location or config.api.location,
    )
    for candidate in search.candidates:
        try:
            matches = matches_criteria(
                config.api, config.website, candidate, search, reference_date=reference_date
            )
        except SearchError:
            # Search summaries omit facts. Defer those candidates to the detail check.
            matches = True
        if matches:
            with SetWorkflowID(f"property:{candidate.property.id}"):
                await DBOS.enqueue_workflow_async(
                    PROPERTY_QUEUE, download_property, candidate, config, context, reference_date
                )


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def fetch_property(candidate: Candidate) -> Property:
    runtime = resources()
    property_id = candidate.property.id
    try:
        for summary in await runtime.client.summaries([property_id]):
            enrich_summary(candidate, summary)
    except (FetchError, ValueError, TypeError) as exc:
        runtime.report(f"{property_id}: summary unavailable; using detail page: {exc}")
    response = await runtime.client.get(BASE_URL + f"/{property_id}")
    return await asyncio.to_thread(parse_property, response.text, str(response.url), candidate)


@DBOS.step()
async def store_property(
    property: Property, commute_minutes: float | None, target: SearchData
) -> None:
    db = resources().db
    if commute_minutes is not None:
        property = replace(
            property,
            openrent_commute_minutes=commute_minutes,
            search_target=target.location,
            search_target_latitude=target.latitude,
            search_target_longitude=target.longitude,
        )
    await database.upsert_property(db, property)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def fetch_amenities(property: Property) -> None:
    runtime = resources()
    fetchers: tuple[MetadataFetcher, ...] = (
        GymMetadataFetcher(runtime.amenity_client),
        GrocerMetadataFetcher(runtime.amenity_client),
    )
    for fetcher in fetchers:
        await fetch_and_store_amenities(runtime.db, property, fetcher)


@DBOS.step()
async def fetch_commute(property: Property, target: SearchData) -> None:
    """Optional journey information must not prevent downloads or review."""
    if not target.location or target.latitude is None or target.longitude is None:
        return
    runtime = resources()
    if runtime.amenity_client is None:
        raise RuntimeError("Amenity HTTP client has not been configured")
    fetcher = CommuteMetadataFetcher(
        runtime.amenity_client, target.location, target.latitude, target.longitude
    )
    try:
        await fetch_and_store_amenities(runtime.db, property, fetcher)
    except FetchError as exc:
        runtime.report(f"{property.id}: public transport estimate unavailable: {exc}")


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def download_image(property_id: int, image: Image) -> None:
    """Checkpoint one download and archive write; no other image progress state."""
    runtime = resources()
    content, mime, dimensions, _ = await runtime.client.image(image.source_url)
    image.width, image.height = dimensions
    await database.store_image(runtime.db, property_id, image, content, mime)


@DBOS.workflow()
async def download_property(
    candidate: Candidate, config: DiscoveryConfig, context: SearchData, reference_date: date
) -> str:
    property = await fetch_property(candidate)
    current = Candidate(property, candidate.distance_km, candidate.commute_minutes)
    if not matches_criteria(
        config.api, config.website, current, context, reference_date=reference_date
    ) or (config.post_filter and not matches_post_filter(property)):
        return "filtered"
    await store_property(property, current.commute_minutes, context)
    await fetch_amenities(property)
    await fetch_commute(property, context)
    if not await post_amenities_filter(property.id):
        return "filtered"
    results = await asyncio.gather(
        *(download_image(property.id, image) for image in property.images),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    if config.review is not None:
        with SetWorkflowID(f"review:{property.id}"):
            await DBOS.enqueue_workflow_async(
                REVIEW_QUEUE, review_property, property.id, config.review
            )
    return "downloaded"
