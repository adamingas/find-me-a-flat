"""Grocer discovery, pedestrian routing, and saved metadata contracts."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from archive_helpers import listing

from openrent import db as database
from openrent.amenities import fetch_and_store_amenities
from openrent.amenity_client import AmenityClient
from openrent.client import FetchError
from openrent.grocers import GrocerMetadataFetcher


@asynccontextmanager
async def provider(handler):
    client = AmenityClient(overpass_fallback_url=None, valhalla_fallback_url=None)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._intervals = dict.fromkeys(client._intervals, 0)
    async with client:
        yield client


def shop(osm_id, kind="supermarket", **tags):
    return {
        "type": "node",
        "id": osm_id,
        "lat": 51.442,
        "lon": -0.152,
        "tags": {"shop": kind, **tags},
    }


@pytest.mark.asyncio
async def test_grocery_types_centres_duplicates_and_unreachable_routes():
    supermarket = {
        "type": "way",
        "id": 1,
        "center": {"lat": 51.441, "lon": -0.155},
        "tags": {"name": "Supermarket", "shop": "supermarket", "addr:street": "High Road"},
    }
    grocery = {
        "type": "relation",
        "id": 1,
        "center": {"lat": 51.442, "lon": -0.154},
        "tags": {"shop": "grocery", "operator": "Traditional Grocer"},
    }
    elements = [
        supermarket,
        supermarket,
        shop(1, "convenience", brand="Local Shop"),
        shop(2, "greengrocer", name="Fruit and Veg"),
        grocery,
        shop(3, "food"),
        shop(4, "supermarket", access="private"),
        shop(5, "convenience", access="no"),
        shop(6, "bakery"),
        shop(7, "alcohol"),
        shop(8, "vacant"),
    ]
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("interpreter"):
            query = parse_qs(request.content.decode())["data"][0]
            for kind in ("supermarket", "convenience", "greengrocer", "grocery", "food"):
                assert f'nwr(around:1500,51.440875,-0.154623)["shop"="{kind}"];' in query
            assert "out center tags" in query
            return httpx.Response(200, json={"elements": elements})
        assert str(request.url) == "https://valhalla1.openstreetmap.de/sources_to_targets"
        routing = json.loads(request.content)
        assert routing["costing"] == "pedestrian"
        assert routing["costing_options"] == {"pedestrian": {"walking_speed": 5}}
        assert routing["sources"] == [{"lat": 51.440875, "lon": -0.154623}]
        assert routing["targets"][-2:] == [
            {"lat": 51.442, "lon": -0.154},
            {"lat": 51.441, "lon": -0.155},
        ]
        return httpx.Response(
            200,
            json={
                "sources_to_targets": [
                    [
                        {"to_index": 4, "time": 120, "distance": 0.15},
                        {"to_index": 3, "time": 900, "distance": 1.2},
                        {"to_index": 2, "time": None, "distance": None},
                        {"to_index": 1, "time": 901, "distance": 1.25},
                        {"to_index": 0, "time": 300, "distance": 0.4},
                    ]
                ]
            },
        )

    async with provider(respond) as client:
        payload = await GrocerMetadataFetcher(client).fetch(
            replace(listing(), latitude=51.440875, longitude=-0.154623)
        )
    grocers = payload["grocers"]
    assert len(requests) == 2
    assert [grocer["name"] for grocer in grocers] == [
        "Local Shop",
        "Fruit and Veg",
        "Unnamed grocer",
        "Traditional Grocer",
        "Supermarket",
    ]
    assert [grocer["walking_seconds"] for grocer in grocers] == [300, 901, None, 900, 120]
    assert grocers[-1]["walking_distance_m"] == 150
    assert grocers[-1]["tags"] == supermarket["tags"]


@pytest.mark.asyncio
async def test_multiple_matrix_batches_preserve_target_order():
    elements = [shop(index) for index in range(27)]
    batch_sizes = []

    def respond(request):
        if request.url.path.endswith("interpreter"):
            return httpx.Response(200, json={"elements": elements})
        count = len(json.loads(request.content)["targets"])
        start = sum(batch_sizes)
        batch_sizes.append(count)
        return httpx.Response(
            200,
            json={
                "sources_to_targets": [
                    [
                        {"to_index": index, "time": 100 + start + index, "distance": 0.1}
                        for index in reversed(range(count))
                    ]
                ]
            },
        )

    async with provider(respond) as client:
        payload = await GrocerMetadataFetcher(client).fetch(
            replace(listing(), latitude=51.440875, longitude=-0.154623)
        )
    assert batch_sizes == [25, 2]
    assert [grocer["walking_seconds"] for grocer in payload["grocers"]] == list(range(100, 127))


@pytest.mark.asyncio
async def test_failed_grocer_lookup_is_not_saved_and_complete_empty_result_is_saved_once(tmp_path):
    responses = [
        {"elements": [], "remark": "Query timed out"},
        {"elements": [shop(1)]},
        {"sources_to_targets": [[]]},
        {"elements": []},
    ]

    def respond(request):
        return httpx.Response(200, json=responses.pop(0))

    prop = replace(listing(), latitude=51.440875, longitude=-0.154623)
    async with database.connect(tmp_path / "archive.sqlite") as db, provider(respond) as client:
        await database.upsert_property(db, prop)
        fetcher = GrocerMetadataFetcher(client)
        for _ in range(2):
            with pytest.raises(FetchError):
                await fetch_and_store_amenities(db, prop, fetcher)
            assert await database.get_amenities(db, prop.id) == {}
        await fetch_and_store_amenities(db, prop, fetcher)
        await fetch_and_store_amenities(db, prop, fetcher)
        assert (await database.get_amenities(db, prop.id))["grocer"]["grocers"] == []
        assert responses == []
        assert await database.query(db, "SELECT type, typeof(data) AS storage FROM amenities") == [
            {"type": "grocer", "storage": "blob"}
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("latitude,longitude", [(None, 0), (0, None), (float("nan"), 0), (91, 0)])
async def test_invalid_origin_does_not_query_providers(latitude, longitude):
    def respond(request):
        raise AssertionError("Invalid property coordinates must not reach providers")

    async with provider(respond) as client:
        with pytest.raises(FetchError, match="needs coordinates for the grocer lookup"):
            await GrocerMetadataFetcher(client).fetch(
                replace(listing(), latitude=latitude, longitude=longitude)
            )
