"""Our HTTP adapter's pacing context and bounded response handling."""

import asyncio
import gzip

import httpx
import pytest

from openrent.fetch_transport import ResponseTooLarge
from openrent.http_transport import HttpFetchTransport


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.asyncio
async def test_http_capacity_covers_body_reading_and_is_released_on_cancellation(concurrency):
    entered = asyncio.Queue()
    release = asyncio.Event()
    active = 0
    peak = 0

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release.wait()
            yield b"done"

        async def aclose(self):
            nonlocal active
            active -= 1

    def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.put_nowait(request.url.path)
        return httpx.Response(200, stream=SlowBody())

    transport = HttpFetchTransport(
        concurrency=concurrency, http_transport=httpx.MockTransport(handler)
    )
    tasks = []
    try:
        for number in range(concurrency):
            tasks.append(asyncio.create_task(transport.get(f"https://www.openrent.co.uk/{number}")))
            assert await asyncio.wait_for(entered.get(), 1) == f"/{number}"
        tasks.append(asyncio.create_task(transport.get("https://www.openrent.co.uk/next")))
        await asyncio.sleep(0)
        assert entered.empty()
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert await asyncio.wait_for(entered.get(), 1) == "/next"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks[1:]), 1)
        assert all(response.content == b"done" for response in results)
        assert peak == concurrency
        assert active == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await transport.aclose()


@pytest.mark.asyncio
async def test_concurrent_fetches_keep_their_own_callback_across_redirects():
    barrier = asyncio.Barrier(2)
    paced = {"one": [], "two": []}

    async def handler(request):
        if not request.url.path.endswith("/end"):
            await barrier.wait()
            return httpx.Response(302, headers={"Location": request.url.path + "/end"})
        return httpx.Response(200, content=b"done")

    transport = HttpFetchTransport(http_transport=httpx.MockTransport(handler), concurrency=2)

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
