"""Our HTTP adapter's pacing context and bounded response handling."""

import asyncio
import gzip

import httpx
import pytest

from openrent.fetch_transport import ResponseTooLarge
from openrent.http_transport import HttpFetchTransport


@pytest.mark.asyncio
async def test_concurrent_fetches_keep_their_own_callback_across_redirects():
    barrier = asyncio.Barrier(2)
    paced = {"one": [], "two": []}

    async def handler(request):
        if not request.url.path.endswith("/end"):
            await barrier.wait()
            return httpx.Response(302, headers={"Location": request.url.path + "/end"})
        return httpx.Response(200, content=b"done")

    transport = HttpFetchTransport(http_transport=httpx.MockTransport(handler))

    async def fetch(name):
        async def pace(request):
            paced[name].append(request.url.path)

        await transport.get(f"https://www.openrent.co.uk/{name}", before_request=pace)

    try:
        await asyncio.wait_for(asyncio.gather(fetch("one"), fetch("two")), 2)
        assert paced == {"one": ["/one", "/one/end"], "two": ["/two", "/two/end"]}
    finally:
        await transport.aclose()


@pytest.mark.parametrize("limit", [3, 100])
@pytest.mark.asyncio
async def test_bounded_fetch_limits_decoded_bytes_and_closes_stream(limit):
    class CompressedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield gzip.compress(b"decoded body")
            if limit == 3:
                pytest.fail("The adapter must stop consuming an oversized response")

        async def aclose(self):
            self.closed = True

    stream = CompressedStream()
    transport = HttpFetchTransport(
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
        )
    )
    try:
        if limit == 3:
            with pytest.raises(ResponseTooLarge):
                await transport.get("https://www.openrent.co.uk/image", max_bytes=limit)
        else:
            response = await transport.get("https://www.openrent.co.uk/image", max_bytes=limit)
            assert response.content == b"decoded body"
            assert "content-encoding" not in response.headers
        assert stream.closed
    finally:
        await transport.aclose()
