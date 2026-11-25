"""Client policy tested against our interface, without Chromium or browser RPC."""

import httpx
import pytest
from test_client import png_bytes

from openrent.client import FetchError, OpenRentClient
from openrent.fetch_transport import BeforeRequest, FetchTransport, QueryParams, RequestHeaders


class FakeTransport(FetchTransport):
    def __init__(self, body: bytes, headers=None):
        self.body = body
        self.headers = headers or {}
        self.requests = []
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        params: QueryParams | None = None,
        headers: RequestHeaders | None = None,
        before_request: BeforeRequest | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        request = httpx.Request("GET", url, params=params, headers=headers)
        self.requests.append(request)
        assert before_request is not None
        await before_request(request)
        return httpx.Response(200, content=self.body, headers=self.headers, request=request)

    async def aclose(self) -> None:
        self.closed = True


def client_using(transport: FetchTransport):
    return OpenRentClient(transport=transport, requests_per_second=1_000_000)


@pytest.mark.asyncio
async def test_client_routes_documents_summaries_and_photos_through_interface():
    browser = FakeTransport(b'[{"id": 42}]')
    async with client_using(browser) as client:
        await client.search({"term": "London"})
        assert await client.summaries([42, 43]) == [{"id": 42}]
        browser.body = png_bytes()
        content, mime, dimensions, _ = await client.image("https://imagescdn.openrent.co.uk/a.png")
        assert (content, mime, dimensions) == (browser.body, "image/png", (7, 9))
    assert browser.requests[0].url.params["term"] == "London"
    assert browser.requests[1].url.params.get_list("ids") == ["42", "43"]
    assert browser.requests[1].headers["X-Requested-With"] == "XMLHttpRequest"
    assert len(browser.requests) == 3
    assert browser.closed


@pytest.mark.parametrize(
    "operation,body,headers,error",
    [
        ("summaries", b'[{"missing_id": 42}]', {}, "response format"),
        ("image", b"<html>Error</html>", {}, "invalid image"),
        ("search", b"challenge", {"x-amzn-waf-action": "challenge"}, "browser verification"),
    ],
)
@pytest.mark.asyncio
async def test_client_keeps_validation_and_closes_transport_on_failure(
    operation, body, headers, error
):
    browser = FakeTransport(body, headers)
    with pytest.raises(FetchError, match=error):
        async with client_using(browser) as client:
            if operation == "summaries":
                await client.summaries([42])
            elif operation == "image":
                await client.image("https://imagescdn.openrent.co.uk/a.png")
            else:
                await client.search({})
    assert browser.closed


@pytest.mark.asyncio
async def test_client_rejects_unapproved_source_before_calling_transport():
    browser = FakeTransport(b"")
    async with client_using(browser) as client:
        with pytest.raises(FetchError, match="non-OpenRent"):
            await client.get("https://example.com/")
        assert await client.summaries([]) == []
    assert browser.requests == []
