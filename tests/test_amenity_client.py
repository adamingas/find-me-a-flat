"""Public Overpass failures resend the unchanged query to the local service."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from openrent.amenity_client import OVERPASS_FALLBACK_URL, OVERPASS_URL, AmenityClient
from openrent.client import FetchError

QUERY = '[out:json];nwr(around:1500,51.440875,-0.154623)["shop"="supermarket"];out center tags;'


@asynccontextmanager
async def provider(handler):
    client = AmenityClient()
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._intervals = dict.fromkeys(client._intervals, 0)
    async with client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
async def test_failed_public_request_is_replayed_locally_without_waiting_for_retry_after(status):
    requests = []
    payload = {"elements": [{"type": "node", "id": 1, "lat": 51.44, "lon": -0.15}]}

    def respond(request):
        requests.append(request)
        if str(request.url) == OVERPASS_URL:
            return httpx.Response(status, headers={"Retry-After": "120"})
        return httpx.Response(200, json=payload)

    async with provider(respond) as client:
        result = await asyncio.wait_for(client.request("overpass", data={"data": QUERY}), 1)
        assert result == payload
        # The public service's cooldown must not delay the next local lookup.
        assert (
            await asyncio.wait_for(client.request("overpass", data={"data": QUERY}), 1) == payload
        )
    assert [str(request.url) for request in requests] == [
        OVERPASS_URL,
        OVERPASS_FALLBACK_URL,
        OVERPASS_FALLBACK_URL,
    ]
    assert all(request.method == "POST" for request in requests)
    assert requests[0].content == requests[1].content == requests[2].content


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
async def test_public_transport_error_uses_local_overpass(error):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if str(request.url) == OVERPASS_URL:
            raise error("Provider unavailable", request=request)
        return httpx.Response(200, json={"elements": []})

    async with provider(respond) as client:
        assert await client.request("overpass", data={"data": QUERY}) == {"elements": []}
    assert requests == [OVERPASS_URL, OVERPASS_FALLBACK_URL]


@pytest.mark.asyncio
async def test_successful_empty_result_does_not_query_local_overpass():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"elements": []})

    async with provider(respond) as client:
        assert await client.osm_elements(QUERY) == []
    assert requests == [OVERPASS_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,status", [("overpass", 400), ("overpass", 403), ("valhalla", 503), ("tfl", 429)]
)
async def test_other_failures_do_not_call_local_overpass(service, status):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(status)

    async with provider(respond) as client:
        with pytest.raises(FetchError):
            await client.request(service, data={"data": QUERY})
    assert len(requests) == 1
    assert OVERPASS_FALLBACK_URL not in requests


@pytest.mark.asyncio
async def test_local_failure_raises_instead_of_looping_between_endpoints():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(503)

    async with provider(respond) as client:
        with pytest.raises(FetchError, match="overpass_local"):
            await client.request("overpass", data={"data": QUERY})
    assert requests == [OVERPASS_URL, OVERPASS_FALLBACK_URL]
