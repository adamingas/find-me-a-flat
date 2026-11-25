import asyncio
import base64
import json
import time

import httpx
import pytest
from test_client import png_bytes, with_transport

from openrent.browser_protocol import allowed_url, is_challenge
from openrent.browser_transport import BrowserTransport
from openrent.client import FetchError


@pytest.mark.parametrize(
    "url",
    [
        "http://www.openrent.co.uk/",
        "https://openrent.co.uk.evil.example/",
        "https://localhost/",
        "https://user:password@openrent.co.uk/",
        "https://openrent.co.uk:8443/",
        "file:///etc/passwd",
        "https://[bad/",
    ],
)
def test_browser_rejects_non_openrent_targets(url):
    assert not allowed_url(url)


def test_challenge_detection_does_not_confuse_normal_pages_or_errors():
    assert is_challenge({"x-amzn-waf-action": "captcha"})
    assert is_challenge({"x-amzn-waf-action": "challenge"})
    assert is_challenge({"content-type": "text/html"}, b"Let's confirm you are human")
    assert not is_challenge({"content-type": "text/html"}, b"No properties available")
    assert not is_challenge({"content-type": "text/html"}, b"Server error")


@pytest.mark.asyncio
async def test_http_challenge_is_reported_before_parsing():
    async with with_transport(
        lambda _: httpx.Response(
            202, headers={"x-amzn-waf-action": "challenge"}, text="JavaScript required"
        )
    ) as client:
        with pytest.raises(FetchError, match="browser verification"):
            await client.search({"term": "London"})


@pytest.mark.asyncio
async def test_browser_transport_pauses_replays_and_preserves_binary_response():
    calls, alerts = [], []
    payload = png_bytes()

    async def notify(episode):
        alerts.append(episode)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(423, json={"episode": "challenge-1"})
        return httpx.Response(
            200,
            json={
                "url": "https://imagescdn.openrent.co.uk/photo.png",
                "status": 200,
                "headers": {"content-type": "image/png"},
                "body": base64.b64encode(payload).decode(),
            },
        )

    browser = BrowserTransport(
        "http://localhost:8091",
        "test-token",
        notify,
        poll_seconds=0,
        http_transport=httpx.MockTransport(handler),
    )
    async with with_transport(
        lambda _: pytest.fail("HTTP fallback is forbidden"), transport=browser
    ) as client:
        content, mime, dimensions, _ = await client.image(
            "https://imagescdn.openrent.co.uk/photo.png"
        )
    assert (content, mime, dimensions) == (payload, "image/png", (7, 9))
    assert calls[0] == calls[1]
    assert alerts == ["challenge-1"]


@pytest.mark.asyncio
async def test_challenge_replays_are_paced_even_when_notification_fails():
    events = []

    async def pace(request):
        events.append("pace")

    async def notify(episode):
        events.append("notify")
        raise OSError("Cannot persist alert reservation")

    def handler(request):
        events.append("fetch")
        if events.count("fetch") == 1:
            return httpx.Response(423, json={"episode": "one"})
        return httpx.Response(
            200,
            json={
                "url": "https://www.openrent.co.uk/search",
                "status": 200,
                "headers": {},
                "body": base64.b64encode(b"Results").decode(),
            },
        )

    browser = BrowserTransport(
        "http://localhost:8091",
        "token",
        notify,
        poll_seconds=0,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = await browser.get("https://www.openrent.co.uk/search", before_request=pace)
        assert result.content == b"Results"
        assert events == ["pace", "fetch", "notify", "pace", "fetch"]
    finally:
        await browser.aclose()


@pytest.mark.asyncio
async def test_cancelling_human_wait_releases_transport_lock():
    notified = asyncio.Event()
    verified = False

    async def notify(_):
        notified.set()

    def handler(request):
        if not verified:
            return httpx.Response(423, json={"episode": "waiting"})
        return httpx.Response(
            200,
            json={
                "url": "https://www.openrent.co.uk/search",
                "status": 200,
                "headers": {},
                "body": base64.b64encode(b"Results").decode(),
            },
        )

    browser = BrowserTransport(
        "http://localhost:8091",
        "test-token",
        notify,
        poll_seconds=60,
        http_transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(browser.get("https://www.openrent.co.uk/search"))
    await asyncio.wait_for(notified.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    verified = True
    try:
        response = await asyncio.wait_for(browser.get("https://www.openrent.co.uk/search"), 1)
        assert response.content == b"Results"
    finally:
        await browser.aclose()


@pytest.mark.asyncio
async def test_browser_summary_params_and_rate_limit_are_shared_across_workers():
    starts, queries = [], []

    def handler(request):
        payload = json.loads(request.content)
        starts.append(time.monotonic())
        url = httpx.URL(payload["url"])
        queries.append(url.params.get_list("ids"))
        assert payload["headers"]["X-Requested-With"] == "XMLHttpRequest"
        return httpx.Response(
            200,
            json={
                "url": str(url),
                "status": 200,
                "headers": {"content-type": "application/json"},
                "body": base64.b64encode(b'[{"id": 101}]').decode(),
            },
        )

    browser = BrowserTransport(
        "http://localhost:8091", "token", None, http_transport=httpx.MockTransport(handler)
    )
    async with with_transport(
        lambda _: pytest.fail("HTTP fallback is forbidden"),
        transport=browser,
        concurrency=2,
        requests_per_second=20,
    ) as client:
        results = await asyncio.gather(client.summaries([101, 102]), client.summaries([103]))
    assert results == [[{"id": 101}], [{"id": 101}]]
    assert queries == [["101", "102"], ["103"]]
    assert starts[1] - starts[0] >= 0.045
