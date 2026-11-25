"""Routing policy and ownership, using only our transport interface."""

import asyncio
import time

import httpx
import pytest

from openrent.client import OpenRentClient
from openrent.routing_transport import RoutingFetchTransport


class RecordingTransport:
    def __init__(self, *, close_error=False):
        self.calls = []
        self.closed = 0
        self.close_error = close_error

    async def get(self, url, *, params=None, headers=None, before_request=None, max_bytes=None):
        request = httpx.Request("GET", url, params=params, headers=headers)
        if before_request is not None:
            await before_request(request)
        self.calls.append((request, max_bytes, time.monotonic()))
        return httpx.Response(200, content=b"data", request=request)

    async def aclose(self):
        self.closed += 1
        if self.close_error:
            raise OSError("close failed")


@pytest.mark.asyncio
async def test_router_selects_with_query_and_headers_and_forwards_limits():
    http, browser = RecordingTransport(), RecordingTransport()
    paced = []

    async def pace(request):
        paced.append(str(request.url))

    router = RoutingFetchTransport(
        http,
        browser,
        lambda request: (
            request.url.params.get("view") == "interactive"
            and request.headers.get("Accept") == "text/html"
        ),
    )
    try:
        await router.get("https://www.openrent.co.uk/search", before_request=pace, max_bytes=10)
        await router.get(
            "https://www.openrent.co.uk/search",
            params=[("view", "interactive"), ("ids", "1"), ("ids", "2")],
            headers={"Accept": "text/html"},
            before_request=pace,
            max_bytes=20,
        )
        assert len(http.calls) == len(browser.calls) == 1
        assert http.calls[0][1] == 10
        request, limit, _ = browser.calls[0]
        assert limit == 20
        assert request.url.params.get_list("ids") == ["1", "2"]
        assert request.headers["Accept"] == "text/html"
        assert paced == [str(http.calls[0][0].url), str(request.url)]
    finally:
        await router.aclose()
    assert http.closed == browser.closed == 1


@pytest.mark.asyncio
async def test_mixed_requests_share_client_rate_limit():
    http, browser = RecordingTransport(), RecordingTransport()
    router = RoutingFetchTransport(http, browser, lambda request: request.url.path == "/browser")
    async with OpenRentClient(transport=router, concurrency=2, requests_per_second=20) as client:
        await asyncio.gather(
            client.get("https://www.openrent.co.uk/http"),
            client.get("https://www.openrent.co.uk/browser"),
        )
    times = sorted([http.calls[0][2], browser.calls[0][2]])
    assert times[1] - times[0] >= 0.045
    assert http.closed == browser.closed == 1


@pytest.mark.asyncio
async def test_router_closes_browser_even_if_http_close_fails():
    http, browser = RecordingTransport(close_error=True), RecordingTransport()
    router = RoutingFetchTransport(http, browser, lambda _: False)
    with pytest.raises(OSError, match="close failed"):
        await router.aclose()
    assert http.closed == browser.closed == 1


@pytest.mark.asyncio
async def test_router_closes_a_shared_child_only_once():
    child = RecordingTransport()
    await RoutingFetchTransport(child, child, lambda _: False).aclose()
    assert child.closed == 1
