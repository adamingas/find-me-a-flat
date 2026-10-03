import asyncio
import io
import threading
import time
from contextlib import asynccontextmanager
from itertools import pairwise

import httpx
import pytest
from PIL import Image

from openrent.client import FetchError, OpenRentClient


@asynccontextmanager
async def with_transport(handler, **kwargs):
    kwargs.setdefault("requests_per_second", 1_000_000)
    client = OpenRentClient(**kwargs)
    headers, cookies, event_hooks = (
        client.http.headers,
        client.http.cookies,
        client.http.event_hooks,
    )
    await client.http.aclose()
    client.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers=headers,
        cookies=cookies,
        event_hooks=event_hooks,
    )
    async with client:
        yield client


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (7, 9)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_summary_api_uses_repeated_ids_and_checks_shape():
    def handler(request):
        assert request.url.path == "/search/propertiesbyid"
        assert request.url.params.get_list("ids") == ["101", "102"]
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        return httpx.Response(200, json=[{"id": 101}, {"id": 102}])

    async def scenario():
        async with with_transport(handler) as client:
            assert len(await client.summaries([101, 102])) == 2

    asyncio.run(scenario())


def test_rate_limit_covers_images_redirects_and_retries():
    starts = []

    def handler(request):
        starts.append((request.url.path, time.monotonic()))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/finish"})
        if request.url.path == "/retry" and sum(path == "/retry" for path, _ in starts) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=png_bytes())

    async def scenario():
        async with with_transport(handler, concurrency=3, requests_per_second=20) as client:
            await asyncio.gather(
                client.get("https://www.openrent.co.uk/redirect"),
                client.get("https://www.openrent.co.uk/retry"),
                client.image("https://imagescdn.openrent.co.uk/photo.png"),
            )

    asyncio.run(scenario())
    assert sorted(path for path, _ in starts) == ["/finish", "/photo.png", "/redirect", "/retry", "/retry"]
    assert all(later[1] - earlier[1] >= 0.045 for earlier, later in pairwise(starts))


def test_cancelled_rate_limit_wait_does_not_block_next_request():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200)

    async def scenario():
        async with with_transport(handler, concurrency=1, requests_per_second=20) as client:
            await client.get("https://www.openrent.co.uk/first")
            task = asyncio.create_task(client.get("https://www.openrent.co.uk/cancel"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(client.get("https://www.openrent.co.uk/last"), 2)

    asyncio.run(scenario())
    assert paths == ["/first", "/last"]


def test_image_validates_bytes_and_dimensions():
    data = png_bytes()

    async def scenario():
        async with with_transport(lambda _: httpx.Response(200, content=data)) as client:
            content, mime, dimensions, _ = await client.image(
                "https://imagescdn.openrent.co.uk/a.png"
            )
        assert content == data
        assert mime == "image/png"
        assert dimensions == (7, 9)

    asyncio.run(scenario())


def test_image_rejects_successful_http_error_page():
    async def scenario():
        async with with_transport(
            lambda _: httpx.Response(200, text="<html>Login required</html>")
        ) as client:
            with pytest.raises(FetchError, match="invalid image"):
                await client.image("https://imagescdn.openrent.co.uk/a.jpg")

    asyncio.run(scenario())


def test_permanent_http_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    async def scenario():
        async with with_transport(handler) as client:
            with pytest.raises(FetchError):
                await client.get("https://www.openrent.co.uk/101")

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("method,status", [("get", 401), ("image", 403)])
def test_access_refusal_is_not_retried(status, method):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    async def scenario():
        async with with_transport(handler) as client:
            with pytest.raises(FetchError, match="cookie-file"):
                await getattr(client, method)("https://www.openrent.co.uk/101")

    asyncio.run(scenario())
    assert len(calls) == 1


def test_transient_http_error_retries(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, text="okay")

    async def no_backoff(*args):
        pass

    async def scenario():
        async with with_transport(handler) as client:
            monkeypatch.setattr("openrent.client.asyncio.sleep", no_backoff)
            assert (await client.get("https://www.openrent.co.uk/101")).text == "okay"

    asyncio.run(scenario())
    assert len(calls) == 2


def test_transport_error_retries(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(200)

    async def no_backoff(*args):
        pass

    async def scenario():
        async with with_transport(handler) as client:
            monkeypatch.setattr("openrent.client.asyncio.sleep", no_backoff)
            assert (await client.get("https://www.openrent.co.uk/101")).status_code == 200

    asyncio.run(scenario())
    assert len(calls) == 2


def test_image_retry_closes_failed_stream(monkeypatch):
    calls = []
    streams = []

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield png_bytes()

        async def aclose(self):
            self.closed = True

    def handler(request):
        calls.append(request)
        stream = Stream()
        streams.append(stream)
        return httpx.Response(503 if len(calls) == 1 else 200, stream=stream)

    async def no_backoff(*args):
        assert streams[0].closed

    async def scenario():
        async with with_transport(handler) as client:
            monkeypatch.setattr("openrent.client.asyncio.sleep", no_backoff)
            assert (await client.image("https://imagescdn.openrent.co.uk/a.png"))[1] == "image/png"

    asyncio.run(scenario())
    assert len(calls) == 2
    assert all(stream.closed for stream in streams)


def test_redirects_follow_valid_hosts():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/intermediate"})
        if request.url.path == "/intermediate":
            return httpx.Response(303, headers={"Location": "/finish"})
        return httpx.Response(200)

    async def scenario():
        async with with_transport(handler) as client:
            redirected, other = await asyncio.gather(
                client.get("https://www.openrent.co.uk/start"),
                client.get("https://www.openrent.co.uk/other"),
            )
            assert redirected.url.path == "/finish"
            assert other.status_code == 200

    asyncio.run(scenario())
    assert sorted(paths) == ["/finish", "/intermediate", "/other", "/start"]


def test_refuses_redirects_to_arbitrary_hosts():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://example.com/listing"})

    async def scenario():
        async with with_transport(handler) as client:
            with pytest.raises(FetchError, match="non-OpenRent"):
                await client.get("https://www.openrent.co.uk/101")

    asyncio.run(scenario())
    assert calls == ["www.openrent.co.uk"]


@pytest.mark.parametrize("method", ["get", "image"])
@pytest.mark.parametrize("succeeds", [True, False])
def test_429_retries_with_exponential_backoff(monkeypatch, method, succeeds):
    calls, waits = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(200 if succeeds and len(calls) == 6 else 429, content=png_bytes())

    async def sleep(seconds):
        waits.append(seconds)

    async def scenario():
        monkeypatch.setattr("openrent.client.asyncio.sleep", sleep)
        async with with_transport(handler) as client:
            if succeeds:
                await getattr(client, method)("https://www.openrent.co.uk/retry")
            else:
                with pytest.raises(FetchError):
                    await getattr(client, method)("https://www.openrent.co.uk/retry")

    asyncio.run(scenario())
    assert len(calls) == 6
    assert waits == [30, 60, 120, 240, 300]


@pytest.mark.parametrize("method", ["get", "image"])
@pytest.mark.parametrize("header,wait", [("61", 61), ("Thu, 01 Jan 1970 00:01:01 GMT", 61), ("invalid", 30)])
def test_retry_after_only_delays_failed_request(monkeypatch, method, header, wait):
    async def scenario():
        sleeping, release = asyncio.Event(), asyncio.Event()
        calls, waits = [], []

        def handler(request):
            calls.append(request.url.path)
            if calls == ["/retry"]:
                return httpx.Response(429, headers={"Retry-After": header})
            return httpx.Response(200, content=png_bytes())

        async def sleep(seconds):
            waits.append(seconds)
            sleeping.set()
            await release.wait()

        monkeypatch.setattr("openrent.client.asyncio.sleep", sleep)
        monkeypatch.setattr("openrent.client.time.time", lambda: 0)
        async with with_transport(handler, concurrency=1) as client:
            retry = asyncio.create_task(getattr(client, method)("https://www.openrent.co.uk/retry"))
            try:
                await asyncio.wait_for(sleeping.wait(), 2)
                response = await asyncio.wait_for(client.get("https://www.openrent.co.uk/other"), 2)
                assert response.status_code == 200
            finally:
                release.set()
                await retry
        assert calls == ["/retry", "/other", "/retry"]
        assert waits == [wait]

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["get", "image"])
def test_cancellation_closes_response(method):
    async def scenario():
        streaming = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                streaming.set()
                await asyncio.Event().wait()
                yield b"never reached"

            async def aclose(self):
                self.closed = True

        stream = WaitingStream()

        def handler(request):
            return (
                httpx.Response(200, stream=stream)
                if request.url.path == "/cancel"
                else httpx.Response(200)
            )

        async with with_transport(handler) as client:
            task = asyncio.create_task(getattr(client, method)("https://www.openrent.co.uk/cancel"))
            await asyncio.wait_for(streaming.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed
            response = await asyncio.wait_for(client.get("https://www.openrent.co.uk/other"), 2)
            assert response.status_code == 200

    asyncio.run(scenario())


def test_oversized_image_closes_response(monkeypatch):
    monkeypatch.setattr("openrent.client.MAX_IMAGE_BYTES", 3)

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"four"

        async def aclose(self):
            self.closed = True

    stream = Stream()

    async def scenario():
        async with with_transport(lambda _: httpx.Response(200, stream=stream)) as client:
            with pytest.raises(FetchError, match="25 MiB"):
                await client.image("https://imagescdn.openrent.co.uk/a.png")
        assert stream.closed

    asyncio.run(scenario())


def test_image_verification_does_not_block_the_event_loop(monkeypatch):
    verification_started, release = threading.Event(), threading.Event()

    def verify(content, url):
        verification_started.set()
        assert release.wait(timeout=3)
        return "image/png", (7, 9)

    async def scenario():
        async with with_transport(lambda _: httpx.Response(200, content=png_bytes()), concurrency=2) as client:
            monkeypatch.setattr(client, "_verify_image", verify)
            task = asyncio.create_task(client.image("https://imagescdn.openrent.co.uk/a.png"))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(verification_started.wait, 2), 3)
                response = await asyncio.wait_for(client.get("https://www.openrent.co.uk/101"), 1)
                assert response.status_code == 200
            finally:
                release.set()
                assert (await task)[1] == "image/png"

    asyncio.run(scenario())


def test_netscape_cookie_file_is_loaded(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".openrent.co.uk\tTRUE\t/\tTRUE\t2147483647\tsession\texample\n"
    )

    def handler(request):
        assert request.headers["Cookie"] == "session=example"
        return httpx.Response(200)

    async def scenario():
        async with with_transport(handler, cookie_file=cookie_file) as client:
            await client.get("https://www.openrent.co.uk/101")

    asyncio.run(scenario())
