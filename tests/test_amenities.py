"""Saved amenity payloads, the fixed filter, and offline gym-provider contracts."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from archive_helpers import gym_payload, listing, store_listing
from dbos import DBOS
from pydantic import ValidationError

from openrent import db as database
from openrent.amenities import fetch_and_store_amenities
from openrent.client import FetchError
from openrent.filtering import post_amenities_filter
from openrent.gyms import AmenityClient, GymAmenities, GymMetadataFetcher
from openrent.review_prompt import render_listing


@DBOS.workflow()
async def filter_property(property_id):
    return await post_amenities_filter(property_id)


@asynccontextmanager
async def provider(handler):
    client = AmenityClient(overpass_fallback_url=None, valhalla_fallback_url=None)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._intervals = dict.fromkeys(client._intervals, 0)
    async with client:
        yield client


@pytest.mark.asyncio
async def test_payload_is_validated_saved_once_and_preserved_on_property_update(tmp_path):
    payload = gym_payload(284, 519, None)

    class Fetcher:
        type = "gym"
        model = GymAmenities

        def __init__(self):
            self.calls = []

        async def fetch(self, property):
            self.calls.append(property.id)
            return payload

    fetcher = Fetcher()
    prop = listing()
    async with database.connect(tmp_path / "archive.sqlite") as db:
        await database.upsert_property(db, prop)
        await fetch_and_store_amenities(db, prop, fetcher)
        assert await database.get_amenities(db, prop.id) == {"gym": payload}
        assert not await database.store_amenity(db, prop.id, "gym", json.dumps(gym_payload(1)))
        await database.upsert_property(db, replace(prop, title="Updated listing", features=[]))
        await fetch_and_store_amenities(db, prop, fetcher)
        assert fetcher.calls == [prop.id]
        assert await database.get_amenities(db, prop.id) == {"gym": payload}
        assert await database.query(db, "SELECT typeof(data) AS storage FROM amenities") == [
            {"storage": "blob"}
        ]
        other = listing(2)
        await database.upsert_property(db, other)
        payload["gyms"][0]["walking_seconds"] = -1
        with pytest.raises(ValidationError):
            await fetch_and_store_amenities(db, other, fetcher)
        assert await database.get_amenities(db, other.id) == {}


@pytest.mark.asyncio
async def test_reviewer_receives_every_amenity_payload_as_json(tmp_path):
    amenities = {
        "gym": gym_payload(284, 519),
        "park": {"places": [{"name": "Common", "paths": ["north", "south"]}]},
    }
    async with database.connect(tmp_path / "archive.sqlite") as db:
        await store_listing(db, listing())
        before = await database.get_evidence(db, 1)
        for amenity_type, payload in amenities.items():
            await database.store_amenity(db, 1, amenity_type, json.dumps(payload))
        evidence = await database.get_evidence(db, 1)
        assert evidence.data["amenities"] == amenities
        assert evidence.fingerprint != before.fingerprint
        prompt = render_listing(evidence.data, evidence.images)
        encoded = prompt.split("Amenities (JSON):\n", 1)[1].splitlines()[0]
        assert json.loads(encoded) == amenities


@pytest.mark.asyncio
async def test_fixed_filter_uses_nearest_gym_and_strict_thirteen_minute_limit(pipeline):
    cases = [
        ((), False),
        ((None,), False),
        ((780,), False),
        ((779.9,), True),
        ((900, None, 284), True),
        ((0,), True),
    ]
    for property_id, (times, expected) in enumerate(cases, 1):
        await database.upsert_property(pipeline.db, listing(property_id))
        await database.store_amenity(
            pipeline.db, property_id, "gym", json.dumps(gym_payload(*times))
        )
        # Another amenity type containing the same JSON keys must not affect the gym rule.
        await database.store_amenity(pipeline.db, property_id, "park", json.dumps(gym_payload(1)))
        handle = await DBOS.start_workflow_async(filter_property, property_id)
        assert await handle.get_result() is expected
    missing = await DBOS.start_workflow_async(filter_property, 999)
    assert await missing.get_result() is False


@pytest.mark.asyncio
async def test_fixed_filter_rejects_only_known_tfl_commutes_over_thirty_minutes(pipeline):
    cases = [(None, True), (0, True), (29, True), (30, True), (31, False)]
    for property_id, (minutes, expected) in enumerate(cases, 1):
        await database.upsert_property(
            pipeline.db, replace(listing(property_id), openrent_commute_minutes=45)
        )
        await database.store_amenity(pipeline.db, property_id, "gym", json.dumps(gym_payload(284)))
        await database.store_amenity(
            pipeline.db,
            property_id,
            "commute",
            json.dumps({"target": "Victoria", "duration_minutes": minutes}),
        )
        handle = await DBOS.start_workflow_async(filter_property, property_id)
        assert await handle.get_result() is expected


@pytest.mark.asyncio
async def test_leisure_centre_is_found_and_routes_are_assigned_by_target_index():
    centre = {
        "type": "way",
        "id": 116444778,
        "center": {"lat": 51.442, "lon": -0.152},
        "tags": {"name": "Balham Leisure Centre", "leisure": "sports_centre", "sport": "multi"},
    }
    elements = [
        centre,
        centre,  # Overlapping OSM selections must not duplicate a target.
        {
            "type": "node",
            "id": 2,
            "lat": 51.445,
            "lon": -0.156,
            "tags": {"name": "Fitness First", "leisure": "fitness_centre"},
        },
        {
            "type": "node",
            "id": 3,
            "lat": 51.441,
            "lon": -0.154,
            "tags": {"name": "Yoga Studio", "leisure": "fitness_centre", "sport": "yoga"},
        },
        {
            "type": "node",
            "id": 4,
            "lat": 51.441,
            "lon": -0.154,
            "tags": {"name": "Tennis Centre", "leisure": "sports_centre", "sport": "tennis"},
        },
    ]
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("interpreter"):
            query = parse_qs(request.content.decode())["data"][0]
            assert '["leisure"="sports_centre"]' in query
            assert "around:1500,51.440875,-0.154623" in query
            return httpx.Response(200, json={"elements": elements})
        assert str(request.url) == "https://valhalla1.openstreetmap.de/sources_to_targets"
        routing = json.loads(request.content)
        assert routing["costing"] == "pedestrian"
        assert routing["costing_options"] == {"pedestrian": {"walking_speed": 5}}
        assert routing["sources"] == [{"lat": 51.440875, "lon": -0.154623}]
        assert routing["targets"] == [
            {"lat": 51.445, "lon": -0.156},
            {"lat": 51.442, "lon": -0.152},
        ]
        return httpx.Response(
            200,
            json={
                "sources_to_targets": [
                    [
                        {"to_index": 1, "time": 284, "distance": 0.394},
                        {"to_index": 0, "time": 519, "distance": 0.721},
                    ]
                ]
            },
        )

    async with provider(respond) as client:
        payload = await GymMetadataFetcher(client).fetch(
            replace(listing(), latitude=51.440875, longitude=-0.154623)
        )
    gyms = payload["gyms"]
    assert len(requests) == 2
    assert [gym["name"] for gym in gyms] == ["Fitness First", "Balham Leisure Centre"]
    assert [gym["walking_seconds"] for gym in gyms] == [519, 284]
    assert gyms[1]["walking_distance_m"] == 394
    assert gyms[1]["tags"] == centre["tags"]


@pytest.mark.asyncio
async def test_failed_lookups_are_not_cached_but_complete_empty_results_are(tmp_path):
    prop = replace(listing(), latitude=51.440875, longitude=-0.154623)
    element = {
        "type": "node",
        "id": 1,
        "lat": 51.441,
        "lon": -0.155,
        "tags": {"leisure": "fitness_centre"},
    }
    responses = [
        (503, {}),
        (200, {"elements": [], "remark": "Query timed out"}),
        (200, {"elements": [element]}),
        (200, {"sources_to_targets": [[]]}),
        (200, {"elements": []}),
    ]
    requests = []

    def respond(request):
        requests.append(request)
        status, payload = responses.pop(0)
        return httpx.Response(status, json=payload, headers={"Retry-After": "0"})

    async with database.connect(tmp_path / "archive.sqlite") as db, provider(respond) as client:
        await database.upsert_property(db, prop)
        fetcher = GymMetadataFetcher(client)
        for _ in range(3):
            with pytest.raises(FetchError):
                await fetch_and_store_amenities(db, prop, fetcher)
            assert await database.get_amenities(db, prop.id) == {}
            client._next_request["overpass"] = 0
        await fetch_and_store_amenities(db, prop, fetcher)
        await fetch_and_store_amenities(db, prop, fetcher)
        assert (await database.get_amenities(db, prop.id))["gym"]["gyms"] == []
        assert len(requests) == 5
        assert responses == []
