"""Discovery, property downloads, and the durable handoff to AI review."""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dbos import DBOS, SetWorkflowID

from . import db as database
from .amenities import MetadataFetcher, fetch_and_store_amenities
from .client import BASE_URL, FetchError
from .commute import CommuteMetadataFetcher
from .filtering import matches_post_filter, post_amenities_filter
from .grocers import GrocerMetadataFetcher
from .gyms import GymMetadataFetcher
from .http_logging import log_http_failure
from .models import Candidate, Image, Property, SearchData
from .parsing import ParseError, enrich_summary, parse_property, parse_search
from .review import ReviewConfig, review_property
from .runtime import resources
from .search import ApiFilters, SearchError, WebsiteFilters, matches_criteria

logger = logging.getLogger(__name__)

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
    logger.info("Searching %s ...", config.api.location)
    response = await runtime.client.search(config.api.parameters())
    logger.info(
        "Search response: status=%s content_type=%r characters=%s path=%r",
        getattr(response, "status_code", "unknown"),
        getattr(response, "headers", {}).get("content-type", "unknown"),
        len(response.text),
        urlsplit(str(response.url)).path,
    )
    try:
        search = await asyncio.to_thread(
            parse_search, response.text, str(response.url), reference_date=reference_date
        )
    except ParseError:
        log_http_failure(response, "search_parse_failed")
        logger.error("Search parsing failed; this attempt produced no candidates for review/email.")
        raise
    if (
        config.api.radius_minutes is not None
        and "minute" not in (search.distance_unit or "").lower()
    ):
        log_http_failure(response, "commute_results_missing")
        raise SearchError(
            "OpenRent did not return commute results; use --radius-distance outside London."
        )
    logger.info("Search parsed: %s candidates.", len(search.candidates))
    return search


@DBOS.workflow()
async def discover(scheduled_time: datetime, config: DiscoveryConfig) -> None:
    reference_date = scheduled_time.astimezone(ZoneInfo("Europe/London")).date()
    logger.info(
        "Discovery started: scheduled_time=%s reference_date=%s.",
        scheduled_time.isoformat(),
        reference_date,
    )
    search = await search_properties(config, reference_date)
    context = SearchData(
        [],
        latitude=search.latitude,
        longitude=search.longitude,
        location=search.location or config.api.location,
    )
    submitted = 0
    for candidate in search.candidates:
        try:
            matches = matches_criteria(
                config.api, config.website, candidate, search, reference_date=reference_date
            )
        except SearchError:
            # Search summaries omit facts. Defer those candidates to the detail check.
            logger.info(
                "%s: search facts incomplete; deferring filters to detail page.",
                candidate.property.id,
            )
            matches = True
        if matches:
            with SetWorkflowID(f"property:{candidate.property.id}"):
                await DBOS.enqueue_workflow_async(
                    PROPERTY_QUEUE, download_property, candidate, config, context, reference_date
                )
            submitted += 1
    logger.info(
        "Discovery complete: candidates=%s filtered=%s submitted=%s. Existing property workflows are reused; downloads and reviews run separately.",
        len(search.candidates),
        len(search.candidates) - submitted,
        submitted,
    )


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def fetch_property(candidate: Candidate) -> Property:
    runtime = resources()
    property_id = candidate.property.id
    logger.info("%s: fetching summary and detail page.", property_id)
    try:
        for summary in await runtime.client.summaries([property_id]):
            enrich_summary(candidate, summary)
    except (FetchError, ValueError, TypeError) as exc:
        logger.warning("%s: summary unavailable; using detail page: %s", property_id, exc)
    response = await runtime.client.get(BASE_URL + f"/{property_id}")
    logger.info("%s: detail response received; parsing.", property_id)
    try:
        return await asyncio.to_thread(parse_property, response.text, str(response.url), candidate)
    except ParseError:
        log_http_failure(response, "detail_parse_failed")
        raise


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
    logger.info("%s: property saved.", property.id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def fetch_amenities(property: Property) -> None:
    runtime = resources()
    fetchers: tuple[MetadataFetcher, ...] = (
        GymMetadataFetcher(runtime.amenity_client),
        GrocerMetadataFetcher(runtime.amenity_client),
    )
    for fetcher in fetchers:
        logger.info("%s: %s lookup/cache check started.", property.id, fetcher.type)
        await fetch_and_store_amenities(runtime.db, property, fetcher)
        logger.info("%s: %s metadata ready.", property.id, fetcher.type)


@DBOS.step()
async def fetch_commute(property: Property, target: SearchData) -> None:
    """Optional journey information must not prevent downloads or review."""
    if not target.location or target.latitude is None or target.longitude is None:
        logger.info("%s: commute skipped; search target coordinates unavailable.", property.id)
        return
    runtime = resources()
    if runtime.amenity_client is None:
        raise RuntimeError("Amenity HTTP client has not been configured")
    fetcher = CommuteMetadataFetcher(
        runtime.amenity_client, target.location, target.latitude, target.longitude
    )
    try:
        logger.info("%s: commute lookup/cache check started.", property.id)
        await fetch_and_store_amenities(runtime.db, property, fetcher)
        logger.info("%s: commute metadata ready.", property.id)
    except FetchError as exc:
        logger.warning("%s: public transport estimate unavailable: %s", property.id, exc)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5)
async def download_image(property_id: int, image: Image) -> None:
    """Checkpoint one download and archive write; no other image progress state."""
    runtime = resources()
    logger.info("%s: image position=%s download started.", property_id, image.position)
    content, mime, dimensions, _ = await runtime.client.image(image.source_url)
    image.width, image.height = dimensions
    await database.store_image(runtime.db, property_id, image, content, mime)
    logger.info("%s: image position=%s saved; bytes=%s.", property_id, image.position, len(content))


@DBOS.workflow()
async def download_property(
    candidate: Candidate, config: DiscoveryConfig, context: SearchData, reference_date: date
) -> str:
    logger.info("%s: property workflow started.", candidate.property.id)
    property = await fetch_property(candidate)
    current = Candidate(property, candidate.distance_km, candidate.commute_minutes)
    if not matches_criteria(
        config.api, config.website, current, context, reference_date=reference_date
    ) or (config.post_filter and not matches_post_filter(property)):
        logger.info("%s: filtered out by listing criteria or EPC/Tube rule.", property.id)
        return "filtered"
    logger.info("%s: listing filters passed; saving and enriching.", property.id)
    await store_property(property, current.commute_minutes, context)
    await fetch_amenities(property)
    await fetch_commute(property, context)
    if not await post_amenities_filter(property.id):
        logger.info("%s: filtered out by gym/commute rule.", property.id)
        return "filtered"
    logger.info(
        "%s: amenity filters passed; downloading %s images.", property.id, len(property.images)
    )
    results = await asyncio.gather(
        *(download_image(property.id, image) for image in property.images),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.error(
                "%s: image stage failed (%s); review not queued.",
                property.id,
                type(result).__name__,
            )
            raise result
    if config.review is not None:
        with SetWorkflowID(f"review:{property.id}"):
            await DBOS.enqueue_workflow_async(
                REVIEW_QUEUE, review_property, property.id, config.review
            )
        logger.info("%s: review submitted as review:%s.", property.id, property.id)
    logger.info(
        "%s: property workflow complete; images=%s review_enabled=%s.",
        property.id,
        len(property.images),
        config.review is not None,
    )
    return "downloaded"
