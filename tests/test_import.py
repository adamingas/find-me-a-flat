"""Download admission and optional summary fallback use real DBOS workflows."""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from dbos import DBOS

from openrent import db as database
from openrent import ingestion
from openrent.client import FetchError
from openrent.models import Candidate, Property, SearchData
from openrent.search import ApiFilters, SearchError, WebsiteFilters

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_failure", ["http", "invalid_identity"])
async def test_summary_failure_falls_back_to_detail_and_archives_complete_gallery(
    pipeline, monkeypatch, summary_failure
):
    async def summaries(ids):
        if summary_failure == "http":
            raise FetchError("Temporary summary failure")
        return [{"id": 999}]

    async def detail(url):
        return SimpleNamespace(text=(FIXTURES / "detail.html").read_text(), url=url)

    monkeypatch.setattr(pipeline.client, "summaries", summaries)
    monkeypatch.setattr(pipeline.client, "get", detail)
    candidate = Candidate(Property(2865841, "https://www.openrent.co.uk/2865841"))
    handle = await DBOS.start_workflow_async(
        ingestion.download_property,
        candidate,
        ingestion.DiscoveryConfig(ApiFilters(), WebsiteFilters()),
        SearchData([]),
        date(2026, 10, 1),
    )
    assert await handle.get_result() == "downloaded"
    row = await database.get_property(pipeline.db, 2865841)
    assert row["bedrooms"] == 1 and row["rent_pcm_pence"] == 285000
    evidence = await database.get_evidence(pipeline.db, 2865841)
    assert len(evidence.images) == 3
    assert all(image.content == image.source_url.encode() for image in evidence.images)


@pytest.mark.asyncio
async def test_missing_search_facts_are_resolved_by_detail_before_filtering(pipeline, monkeypatch):
    candidate = Candidate(
        Property(2865841, "https://www.openrent.co.uk/2865841", is_shared=False, is_studio=False)
    )
    monkeypatch.setattr(ingestion, "parse_search", lambda *args, **kwargs: SearchData([candidate]))

    async def detail(url):
        return SimpleNamespace(text=(FIXTURES / "detail.html").read_text(), url=url)

    monkeypatch.setattr(pipeline.client, "get", detail)
    handle = await DBOS.start_workflow_async(
        ingestion.discover,
        datetime.now(UTC),
        ingestion.DiscoveryConfig(ApiFilters(), WebsiteFilters(bedrooms_min=1)),
    )
    await handle.get_result()
    downloaded = await DBOS.retrieve_workflow_async("property:2865841")
    assert await downloaded.get_result() == "downloaded"
    assert (await database.get_property(pipeline.db, 2865841))["bedrooms"] == 1


@pytest.mark.asyncio
async def test_commute_search_refuses_geographic_results_without_downloading(pipeline, monkeypatch):
    monkeypatch.setattr(
        ingestion, "parse_search", lambda *args, **kwargs: SearchData([], distance_unit="km")
    )
    # Test admission without waiting through the step's configured network retries.
    with pytest.raises(SearchError, match="did not return commute results"):
        await ingestion.search_properties.__wrapped__(
            ingestion.DiscoveryConfig(
                ApiFilters(location="Cambridge", radius_minutes=15), WebsiteFilters()
            ),
            date(2026, 10, 1),
        )
    assert pipeline.client.downloaded == []
