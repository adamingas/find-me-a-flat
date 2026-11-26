"""Offline browser tests: OPENRENT_BROWSER_TESTS=1 uv run --extra browser pytest ..."""

import asyncio
import base64
import json
import logging
import os

import pytest

pytest.importorskip("playwright")
pytest.importorskip("aiohttp")
from aiohttp import ClientSession, web
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from openrent.browser_worker import BrowserConfig, BrowserWorker, application


@pytest.mark.asyncio
async def test_worker_api_requires_auth_and_preserves_upstream_status(caplog):
    class Worker:
        context = episode = None

        async def start(self):
            pass

        async def close(self):
            pass

        async def fetch(self, url, headers):
            if url.endswith("/failure"):
                raise RuntimeError("Cannot start display; token=private-token")
            if url.endswith("/challenge"):
                return {"episode": "one"}
            return {"url": url, "status": 429, "headers": {"retry-after": "30"}, "body": b"Wait"}

    runner = web.AppRunner(application(Worker(), "secret"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await client.get("/status")).status == 401
            client.headers["Authorization"] = "Bearer secret"
            response = await client.get("/status")
            assert response.status == 200
            assert (await response.json())["state"] == "idle"
            response = await client.post(
                "/fetch", json={"url": "https://www.openrent.co.uk/challenge"}
            )
            assert response.status == 423
            assert (await response.json())["episode"] == "one"
            response = await client.post(
                "/fetch", json={"url": "https://www.openrent.co.uk/search"}
            )
            assert response.status == 200
            assert (await response.json())["status"] == 429
            assert (await response.json())["headers"]["retry-after"] == "30"
            response = await client.post(
                "/fetch", json={"url": "https://www.openrent.co.uk/failure"}
            )
            assert response.status == 503
            assert "Cannot start display" in caplog.text
            assert "private-token" not in caplog.text
    finally:
        await runner.cleanup()


@pytest.mark.skipif(
    os.environ.get("OPENRENT_BROWSER_TESTS") != "1", reason="requires installed Chromium"
)
@pytest.mark.asyncio
async def test_real_browser_handoff_persistence_idle_shutdown_and_json(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="openrent.browser_worker")
    navigations = []
    photo = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6XcAAAAASUVORK5CYII="
    )
    desktop = os.environ.get("OPENRENT_DESKTOP_TESTS") == "1"
    password_file = tmp_path / "vnc-password"
    password_file.write_text("testpass")

    class FixtureWorker(BrowserWorker):
        async def _ensure_browser(self):
            previous = self.context
            await super()._ensure_browser()
            if self.context is previous:
                return

            async def fixture(route):
                request = route.request
                if not request.is_navigation_request():
                    await route.abort()
                    return
                navigations.append(request.url)
                if request.url.endswith("/photo.png"):
                    await route.fulfill(status=200, content_type="image/png", body=photo)
                elif request.url.endswith("/silent"):
                    if "automatic=1" in request.headers.get("cookie", ""):
                        await route.fulfill(
                            status=200, content_type="text/html", body="Passed automatically"
                        )
                    else:
                        await route.fulfill(
                            status=202,
                            headers={
                                "content-type": "text/html",
                                "x-amzn-waf-action": "challenge",
                            },
                            body="<script>document.cookie='automatic=1;path=/;secure';location.reload()</script>",
                        )
                elif request.url.endswith("/api"):
                    assert request.headers["x-requested-with"] == "XMLHttpRequest"
                    await route.fulfill(
                        status=200, content_type="application/json", body='[{"id": 42}]'
                    )
                elif "verified=1" in request.headers.get("cookie", ""):
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<script>var PROPERTYIDS=[42]</script><p>Results</p>",
                    )
                else:
                    await route.fulfill(
                        status=202,
                        headers={
                            "content-type": "text/html",
                            "x-amzn-waf-action": "captcha",
                        },
                        body="""<p>Let's confirm you are human</p><button onclick="document.cookie='verified=1;path=/;max-age=3600;secure';location.reload()">Verify</button>""",
                    )

            # Every request is fulfilled or aborted locally. No OpenRent traffic.
            await self.context.route("**/*", fixture)

    worker = FixtureWorker(
        BrowserConfig(
            profile=tmp_path / "profile",
            vnc_password_file=password_file,
            idle_seconds=0.2,
            settle_seconds=0.1,
            desktop=desktop,
            sandbox=False,
        )
    )
    await worker.start()
    try:
        automatic = await worker.fetch("https://www.openrent.co.uk/silent")
        assert automatic["status"] == 200
        assert worker.episode is None
        first = await worker.fetch("https://www.openrent.co.uk/search")
        assert "episode" in first
        if desktop:
            async with ClientSession() as viewer:
                page = await viewer.get("http://127.0.0.1:5800/vnc.html")
                assert page.status == 200
                assert "noVNC" in await page.text()
                async with viewer.ws_connect("http://127.0.0.1:5800/websockify") as socket:
                    assert await socket.receive_bytes() == b"RFB 003.008\n"
                    await socket.send_bytes(b"RFB 003.008\n")
                    # VNC authentication is required; anonymous access is not offered.
                    assert await socket.receive_bytes() == b"\x01\x02"
                    await socket.send_bytes(b"\x02")
                    challenge = await socket.receive_bytes()
                    assert len(challenge) == 16
                    key = bytes(int(f"{b:08b}"[::-1], 2) for b in b"testpass")
                    encrypt = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
                    await socket.send_bytes(encrypt.update(challenge) + encrypt.finalize())
                    assert await socket.receive_bytes() == b"\0\0\0\0"
        count = len(navigations)
        await asyncio.sleep(0.3)
        assert worker.context is not None  # A challenge inhibits idle shutdown.
        assert await worker.fetch("https://www.openrent.co.uk/search") == first
        assert len(navigations) == count  # Polling never reloads the challenge.
        await worker.page.get_by_role("button", name="Verify").click()
        await worker.page.get_by_text("Results", exact=True).wait_for()
        result = await worker.fetch("https://www.openrent.co.uk/search")
        assert result["status"] == 200
        assert b"PROPERTYIDS" in result["body"]
        assert worker.episode is None
        api = await worker.fetch(
            "https://www.openrent.co.uk/api", {"X-Requested-With": "XMLHttpRequest"}
        )
        assert json.loads(api["body"]) == [{"id": 42}]
        image = await worker.fetch("https://imagescdn.openrent.co.uk/photo.png")
        assert image["body"] == photo
        for _ in range(100):
            if worker.context is None:
                break
            await asyncio.sleep(0.05)
        assert worker.context is None
        assert "Browser verification cleared; request resumed" in caplog.text
        assert "episode=" + first["episode"] in caplog.text
        assert "Chromium idle timeout reached" in caplog.text
        result = await worker.fetch("https://www.openrent.co.uk/search")
        assert result["status"] == 200  # Profile cookie survived browser shutdown.
    finally:
        await worker.close()
