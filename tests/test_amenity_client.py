"""Public provider failures resend unchanged requests to their local services."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from openrent import amenity_client
from openrent.amenity_client import (
    OVERPASS_FALLBACK_URL,
    OVERPASS_URL,
    VALHALLA_FALLBACK_URL,
    VALHALLA_URL,
    AmenityClient,
)
from openrent.client import FetchError

QUERY = '[out:json];nwr(around:1500,51.440875,-0.154623)["shop"="supermarket"];out center tags;'
URLS = {
    "overpass": (OVERPASS_URL, OVERPASS_FALLBACK_URL),
    "valhalla": (VALHALLA_URL, VALHALLA_FALLBACK_URL),
}
REQUESTS = {
    "overpass": {"data": {"data": QUERY}},
    "valhalla": {
        "json": {
            "sources": [{"lat": 51.440875, "lon": -0.154623}],
            "targets": [{"lat": 51.442, "lon": -0.152}],
            "costing": "pedestrian",
            "costing_options": {"pedestrian": {"walking_speed": 5}},
            "units": "kilometers",
        }
    },
}


@pytest.fixture(params=URLS)
def service(request):
    return request.param


@asynccontextmanager
async def provider(handler, **kwargs):
    client = AmenityClient(**kwargs)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._intervals = dict.fromkeys(client._intervals, 0)
    async with client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
async def test_failed_public_request_is_replayed_locally_without_waiting_for_retry_after(
    service, status
):
    requests = []
    public_url, local_url = URLS[service]
    payload = {"elements": [{"type": "node", "id": 1, "lat": 51.44, "lon": -0.15}]}

    def respond(request):
        requests.append(request)
        if str(request.url) == public_url:
            return httpx.Response(status, headers={"Retry-After": "120"})
        return httpx.Response(200, json=payload)

    async with provider(respond) as client:
        result = await asyncio.wait_for(client.request(service, **REQUESTS[service]), 1)
        assert result == payload
        # The public service's cooldown must not delay the next local lookup.
        assert await asyncio.wait_for(client.request(service, **REQUESTS[service]), 1) == payload
    assert [str(request.url) for request in requests] == [
        public_url,
        local_url,
        local_url,
    ]
    assert all(request.method == "POST" for request in requests)
    assert requests[0].content == requests[1].content == requests[2].content


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
async def test_public_transport_error_uses_local_service(service, error):
    requests = []
    public_url, local_url = URLS[service]

    def respond(request):
        requests.append(str(request.url))
        if str(request.url) == public_url:
            raise error("Provider unavailable", request=request)
        return httpx.Response(200, json={"elements": []})

    async with provider(respond) as client:
        assert await client.request(service, **REQUESTS[service]) == {"elements": []}
    assert requests == [public_url, local_url]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,retry_after,cooldown",
    [
        (500, None, 30),
        (429, "0", 30),
        (429, "120", 120),
        (503, "120", 120),
        (429, "Thu, 01 Jan 1970 00:02:00 GMT", 120),
    ],
)
async def test_public_service_is_retried_after_its_cooldown(
    service, status, retry_after, cooldown, monkeypatch
):
    now = 100.0
    monkeypatch.setattr(
        amenity_client, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: 0)
    )
    public_url, local_url = URLS[service]
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(
                status, headers={"Retry-After": retry_after} if retry_after else {}
            )
        return httpx.Response(200, json={"ok": True})

    async with provider(respond) as client:
        assert await client.request(service, **REQUESTS[service]) == {"ok": True}
        now += cooldown - 1
        assert await client.request(service, **REQUESTS[service]) == {"ok": True}
        now += 1
        assert await client.request(service, **REQUESTS[service]) == {"ok": True}
    assert requests == [public_url, local_url, local_url, public_url]


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
    "service,status",
    [("overpass", 400), ("overpass", 403), ("valhalla", 400), ("valhalla", 403), ("tfl", 429)],
)
async def test_other_failures_do_not_call_local_services(service, status):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(status)

    async with provider(respond) as client:
        with pytest.raises(FetchError):
            await client.request(service, data={"data": QUERY})
    assert len(requests) == 1
    assert OVERPASS_FALLBACK_URL not in requests
    assert VALHALLA_FALLBACK_URL not in requests


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kwargs", [{"text": "invalid JSON"}, {"json": []}])
async def test_invalid_success_payload_does_not_call_local_service(service, response_kwargs):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, **response_kwargs)

    async with provider(respond) as client:
        with pytest.raises(FetchError):
            await client.request(service, **REQUESTS[service])
    assert requests == [URLS[service][0]]


@pytest.mark.asyncio
async def test_incomplete_matrix_does_not_call_local_valhalla():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"sources_to_targets": [[]]})

    async with provider(respond) as client:
        with pytest.raises(FetchError, match="incomplete pedestrian matrix"):
            await client.walking_routes(51.440875, -0.154623, [(51.442, -0.152)])
    assert requests == [VALHALLA_URL]


@pytest.mark.asyncio
async def test_fallback_can_be_disabled(service):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(503)

    async with provider(respond, **{f"{service}_fallback_url": None}) as client:
        with pytest.raises(FetchError, match=service):
            await client.request(service, **REQUESTS[service])
    assert requests == [URLS[service][0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [False, True])
async def test_local_failure_raises_instead_of_looping_between_endpoints(service, transport_error):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if transport_error and len(requests) == 2:
            raise httpx.ConnectError("Local provider unavailable", request=request)
        return httpx.Response(503)

    async with provider(respond) as client:
        with pytest.raises(FetchError, match=f"{service}_local"):
            await client.request(service, **REQUESTS[service])
    assert requests == list(URLS[service])
