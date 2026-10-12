"""TfL arrival-time routing, persisted estimates, and optional lookup failures."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pytest
from archive_helpers import listing, store_listing
from dbos import DBOS
from pipeline_worker import property_fixture

from openrent import db as database
from openrent import ingestion
from openrent.amenities import fetch_and_store_amenities
from openrent.amenity_client import AmenityClient
from openrent.client import FetchError
from openrent.commute import CommuteMetadataFetcher, next_weekday
from openrent.models import Candidate, SearchData
from openrent.search import ApiFilters, WebsiteFilters


@asynccontextmanager
async def provider(handler):
    client = AmenityClient()
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._intervals["tfl"] = 0
    async with client:
        yield client


def journey(duration, mode="tube", arrival="08:58:00"):
    return {
        "duration": duration,
        "startDateTime": "2026-10-12T08:32:00",
        "arrivalDateTime": f"2026-10-12T{arrival}",
        "legs": [
            {"mode": {"id": "walking"}, "instruction": {"summary": "Walk to the station"}},
            {"mode": {"id": mode}, "instruction": {"summary": f"{mode} to Victoria"}},
        ],
    }


@pytest.mark.parametrize("day", [date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 11)])
def test_weekend_journeys_use_monday(day):
    assert next_weekday(day) == date(2026, 10, 12)


def test_next_weekday_excludes_today():
    assert next_weekday(date(2026, 10, 12)) == date(2026, 10, 13)


@pytest.mark.asyncio
async def test_fastest_transit_before_nine_is_saved_with_walking_and_target(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert (
            request.url.path == "/Journey/JourneyResults/51.49435,-0.0954083/to/51.495812,-0.143826"
        )
        assert dict(request.url.params) == {
            "date": "20261012",
            "time": "0900",
            "timeIs": "Arriving",
            "journeyPreference": "LeastTime",
            "calcOneDirection": "true",
        }
        return httpx.Response(
            200,
            json={
                "journeys": [
                    journey(5, "walking"),
                    journey(20, arrival="09:05:00"),
                    journey(28),
                    journey(26, "bus"),
                ]
            },
        )

    prop = replace(listing(), latitude=51.49435, longitude=-0.0954083)
    async with database.connect(tmp_path / "archive.sqlite") as db, provider(respond) as client:
        await store_listing(db, prop)
        fetcher = CommuteMetadataFetcher(
            client, "Victoria", 51.495812, -0.143826, arrival_date=date(2026, 10, 12)
        )
        await fetch_and_store_amenities(db, prop, fetcher)
        await fetch_and_store_amenities(db, prop, fetcher)
        saved = (await database.get_amenities(db, prop.id))["commute"]
        assert saved["target"] == "Victoria"
        assert saved["duration_minutes"] == 26
        assert saved["arrive_by"] == "2026-10-12T09:00:00+01:00"
        assert saved["departure_time"] == "2026-10-12T08:32:00+01:00"
        assert saved["route"] == ["Walk to the station", "bus to Victoria"]
        assert (await database.get_evidence(db, prop.id)).data["amenities"]["commute"] == saved
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_no_transit_route_has_no_invented_duration():
    async with provider(
        lambda request: httpx.Response(200, json={"journeys": [journey(5, "walking")]})
    ) as client:
        payload = await CommuteMetadataFetcher(
            client, "Victoria", 51.495812, -0.143826, arrival_date=date(2026, 10, 12)
        ).fetch(replace(listing(), latitude=51.49435, longitude=-0.0954083))
        assert payload["duration_minutes"] is None
        assert payload["route"] == []


@pytest.mark.asyncio
async def test_invalid_provider_json_is_not_cached(tmp_path):
    prop = replace(listing(), latitude=51.49435, longitude=-0.0954083)
    async with (
        database.connect(tmp_path / "archive.sqlite") as db,
        provider(
            lambda request: httpx.Response(200, json={"journeys": [{"duration": 1}]})
        ) as client,
    ):
        await database.upsert_property(db, prop)
        with pytest.raises(FetchError, match="invalid commute metadata"):
            await fetch_and_store_amenities(
                db, prop, CommuteMetadataFetcher(client, "Victoria", 51.495812, -0.143826)
            )
        assert await database.get_amenities(db, prop.id) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("openrent_minutes", [0, 22.5, None])
async def test_openrent_minutes_survive_tfl_failure_and_images_continue(
    pipeline, monkeypatch, openrent_minutes
):
    from openrent.runtime import resources

    prop = replace(property_fixture(), latitude=51.49435, longitude=-0.0954083)
    monkeypatch.setattr(ingestion, "parse_property", lambda *args: prop)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={}, headers={"Retry-After": "0"})

    async with provider(respond) as client:
        resources().amenity_client = client
        handle = await DBOS.start_workflow_async(
            ingestion.download_property,
            Candidate(prop, commute_minutes=openrent_minutes),
            ingestion.DiscoveryConfig(ApiFilters(), WebsiteFilters()),
            SearchData([], location="Victoria", latitude=51.495812, longitude=-0.143826),
            datetime.now(UTC).date(),
        )
        assert await handle.get_result() == "downloaded"
    assert len(requests) == 1
    assert len(pipeline.client.downloaded) == 2
    amenities = await database.get_amenities(pipeline.db, prop.id)
    assert "commute" not in amenities
    assert "openrent_commute" not in amenities
    row = await database.get_property(pipeline.db, prop.id)
    assert row["openrent_commute_minutes"] == openrent_minutes
    if openrent_minutes is not None:
        assert row["search_target"] == "Victoria"
        assert row["search_target_latitude"] == 51.495812
        assert row["search_target_longitude"] == -0.143826
    evidence = await database.get_evidence(pipeline.db, prop.id)
    assert evidence.data["openrent_commute_minutes"] == openrent_minutes
