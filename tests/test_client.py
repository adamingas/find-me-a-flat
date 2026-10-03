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
async def with_transport(handler, retries=0, **kwargs):
    client = OpenRentClient(delay=kwargs.pop("delay", 0), retries=retries, **kwargs)
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
        async with with_transport(handler, retries=3) as client:
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
        async with with_transport(handler, retries=3) as client:
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
        async with with_transport(handler, retries=1) as client:
            monkeypatch.setattr(client, "_backoff", no_backoff)
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
        async with with_transport(handler, retries=1) as client:
            monkeypatch.setattr(client, "_backoff", no_backoff)
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
        async with with_transport(handler, retries=1) as client:
            monkeypatch.setattr(client, "_backoff", no_backoff)
            assert (await client.image("https://imagescdn.openrent.co.uk/a.png"))[1] == "image/png"

    asyncio.run(scenario())
    assert len(calls) == 2
    assert all(stream.closed for stream in streams)


def test_requests_share_a_concurrency_limit():
    async def scenario():
        active = maximum = calls = 0
        at_capacity, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            nonlocal active, maximum, calls
            calls += 1
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                at_capacity.set()
            try:
                await release.wait()
                return httpx.Response(200, content=png_bytes())
            finally:
                active -= 1

        async with with_transport(handler, concurrency=2) as client:
            tasks = [
                asyncio.create_task(client.get(f"https://www.openrent.co.uk/{number}"))
                for number in range(3)
            ] + [
                asyncio.create_task(client.image(f"https://imagescdn.openrent.co.uk/{number}.png"))
                for number in range(3)
            ]
            try:
                await asyncio.wait_for(at_capacity.wait(), 2)
                assert calls == active == maximum == 2
            finally:
                release.set()
                await asyncio.gather(*tasks)
        assert calls == 6
        assert maximum == 2

    asyncio.run(scenario())


def test_request_starts_share_one_pacing_interval():
    starts = []

    async def handler(request):
        starts.append(time.monotonic())
        await asyncio.sleep(0.01)
        return httpx.Response(200)

    async def scenario():
        async with with_transport(handler, delay=0.025, concurrency=4) as client:
            await asyncio.gather(
                *(client.get(f"https://www.openrent.co.uk/{number}") for number in range(5))
            )

    asyncio.run(scenario())
    assert len(starts) == 5
    assert all(second - first >= 0.023 for first, second in pairwise(starts))


def test_redirect_hops_share_the_pacing_interval_with_other_requests():
    starts, paths = [], []

    def handler(request):
        starts.append(time.monotonic())
        paths.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/intermediate"})
        if request.url.path == "/intermediate":
            return httpx.Response(303, headers={"Location": "/finish"})
        return httpx.Response(200)

    async def scenario():
        async with with_transport(handler, delay=0.025, concurrency=2) as client:
            redirected, other = await asyncio.gather(
                client.get("https://www.openrent.co.uk/start"),
                client.get("https://www.openrent.co.uk/other"),
            )
            assert redirected.url.path == "/finish"
            assert other.status_code == 200

    asyncio.run(scenario())
    assert sorted(paths) == ["/finish", "/intermediate", "/other", "/start"]
    assert all(second - first >= 0.023 for first, second in pairwise(starts))


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


def test_retry_after_defers_a_worker_already_waiting_for_pacing():
    starts = {}

    async def handler(request):
        starts[request.url.path] = time.monotonic()
        if request.url.path == "/busy":
            await asyncio.sleep(0.03)
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200)

    async def scenario():
        async with with_transport(handler, delay=0.1, concurrency=2) as client:
            busy, okay = await asyncio.gather(
                client.get("https://www.openrent.co.uk/busy"),
                client.get("https://www.openrent.co.uk/okay"),
                return_exceptions=True,
            )
            assert isinstance(busy, FetchError)
            assert okay.status_code == 200

    asyncio.run(scenario())
    assert starts["/okay"] - starts["/busy"] >= 1.0


def test_backoff_releases_network_capacity(monkeypatch):
    async def scenario():
        backoff_started, release = asyncio.Event(), asyncio.Event()
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(503 if calls == ["/retry"] else 200)

        async def backoff(*args):
            backoff_started.set()
            await release.wait()

        async with with_transport(handler, retries=1, concurrency=1) as client:
            monkeypatch.setattr(client, "_backoff", backoff)
            retry = asyncio.create_task(client.get("https://www.openrent.co.uk/retry"))
            try:
                await asyncio.wait_for(backoff_started.wait(), 2)
                response = await asyncio.wait_for(client.get("https://www.openrent.co.uk/other"), 2)
                assert response.status_code == 200
            finally:
                release.set()
                assert (await retry).status_code == 200
        assert calls == ["/retry", "/other", "/retry"]

    asyncio.run(scenario())


def test_long_server_pause_stops_other_workers():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "61"})

    async def scenario():
        async with with_transport(handler) as client:
            with pytest.raises(FetchError, match="61s pause"):
                await client.get("https://www.openrent.co.uk/busy")
            with pytest.raises(FetchError, match="61s pause"):
                await client.get("https://www.openrent.co.uk/other")

    asyncio.run(scenario())
    assert calls == ["/busy"]


@pytest.mark.parametrize("method", ["get", "image"])
def test_cancellation_closes_response_and_releases_capacity(method):
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

        async with with_transport(handler, concurrency=1) as client:
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


def test_image_verification_does_not_block_the_event_loop_or_network_capacity(monkeypatch):
    verification_started, release = threading.Event(), threading.Event()

    def verify(content, url):
        verification_started.set()
        assert release.wait(timeout=3)
        return "image/png", (7, 9)

    async def scenario():
        async with with_transport(
            lambda _: httpx.Response(200, content=png_bytes()), concurrency=1
        ) as client:
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
