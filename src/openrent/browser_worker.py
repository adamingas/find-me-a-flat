"""One lazily started, persistent Chromium session shared with a noVNC viewer.

The worker owns its own child processes. It never needs the Docker socket or
access to the application's archive or email credentials. Its CAPTCHA solver uses
the shared OpenRouter credential through an injected interface.
"""

import asyncio
import base64
import contextlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .browser_protocol import MAX_BODY_BYTES, allowed_url, is_challenge
from .captcha_solver import CaptchaSolver
from .http_logging import safe_error, safe_url
from .logging_setup import configure_logging

logger = logging.getLogger("openrent.browser_worker")


@dataclass
class BrowserConfig:
    profile: Path
    vnc_password_file: Path
    idle_seconds: float = 300
    settle_seconds: float = 15
    request_timeout: float = 60
    sandbox: bool = True
    # Headless operation is only used by offline integration tests.
    desktop: bool = True
    proxy: str | None = None

    def __post_init__(self):
        for value in (self.idle_seconds, self.settle_seconds, self.request_timeout):
            if not 0 < value < float("inf"):
                raise ValueError("Browser timeouts must be positive and finite.")


class BrowserWorker:
    def __init__(self, config, solver: CaptchaSolver | None = None):
        self.config = config
        self.solver = solver
        self._attempted_episode = None
        self.lock = asyncio.Lock()
        self.context = None
        self.page = None
        self.playwright = None
        self.response = None
        self.snapshot = None
        self.episode = None
        self.last_used = time.monotonic()
        self.processes = []
        self.reaper = None

    async def start(self):
        self.reaper = asyncio.create_task(self._reap())

    async def close(self):
        if self.reaper:
            self.reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reaper
        async with self.lock:
            await self._stop_browser()

    async def _reap(self):
        while True:
            await asyncio.sleep(min(30, self.config.idle_seconds))
            async with self.lock:
                # A pending human challenge is deliberately exempt from idle shutdown.
                if (
                    self.context
                    and not self.episode
                    and time.monotonic() - self.last_used >= self.config.idle_seconds
                ):
                    logger.info("Chromium idle timeout reached; stopping browser and viewer.")
                    await self._stop_browser()

    async def _stop_browser(self):
        was_running = self.context is not None or bool(self.processes)
        try:
            if self.context:
                with contextlib.suppress(PlaywrightError, TimeoutError):
                    await asyncio.wait_for(self.context.close(), 10)
        finally:
            self.context = self.page = self.response = self.snapshot = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            for process in reversed(self.processes):
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            self.processes.clear()
            if was_running:
                logger.info("Chromium and viewer stopped; persistent profile retained.")

    async def _start_desktop(self):
        password = self.config.vnc_password_file.read_bytes().strip()
        if not 6 <= len(password) <= 8 or any(c < 33 or c > 126 for c in password):
            raise ValueError("VNC password must contain 6–8 printable ASCII characters.")
        process = await asyncio.create_subprocess_exec(
            "vncpasswd", "-f", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        encoded, _ = await process.communicate(password + b"\n")
        if process.returncode:
            raise RuntimeError("Cannot prepare VNC password")
        password_path = Path("/tmp/openrent-vnc-password")
        password_path.touch(mode=0o600)
        password_path.chmod(0o600)
        password_path.write_bytes(encoded)
        self.processes.append(
            await asyncio.create_subprocess_exec(
                "Xtigervnc",
                ":99",
                "-geometry",
                "1280x900",
                "-depth",
                "24",
                "-localhost",
                "-rfbport",
                "5900",
                "-rfbauth",
                str(password_path),
                "-SecurityTypes",
                "VncAuth",
                "-nolisten",
                "tcp",
                stdout=asyncio.subprocess.DEVNULL,
            )
        )
        for _ in range(100):
            if self.processes[-1].returncode is not None:
                raise RuntimeError("Virtual display exited during startup")
            if Path("/tmp/.X11-unix/X99").exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Virtual display did not start")
        self.processes.append(
            await asyncio.create_subprocess_exec(
                "websockify",
                "--web=/usr/share/novnc",
                "0.0.0.0:5800",
                "127.0.0.1:5900",
                stdout=asyncio.subprocess.DEVNULL,
            )
        )

    async def _ensure_browser(self):
        if self.context and self.page and not self.page.is_closed():
            return
        await self._stop_browser()
        self.config.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config.profile.chmod(0o700)
        try:
            if self.config.desktop:
                await self._start_desktop()
            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.config.profile),
                headless=not self.config.desktop,
                chromium_sandbox=self.config.sandbox,
                proxy={"server": self.config.proxy} if self.config.proxy else None,
                env={**os.environ, "DISPLAY": ":99"},
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
            )
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            await self.context.route("**/*", self._guard_navigation)
            self.page.on("response", self._on_response)
            logger.info("Chromium started with its persistent profile.")
        except BaseException:
            await self._stop_browser()
            raise

    async def _guard_navigation(self, route):
        request = route.request
        # Third-party CAPTCHA frames/scripts remain available.
        if (
            request.is_navigation_request()
            and request.frame == self.page.main_frame
            and not allowed_url(request.url)
        ):
            await route.abort()
            return
        await route.continue_()

    def _on_response(self, response):
        request = response.request
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            self.response = response
            self.snapshot = None

    async def _snapshot(self):
        response = self.response
        if not response or not allowed_url(response.url):
            return None
        if self.snapshot is not None:
            return self.snapshot
        headers = await response.all_headers()
        # Browser bodies are decoded; do not pass compression/length headers to httpx.
        headers = {
            k: v
            for k, v in headers.items()
            if k in {"content-type", "retry-after", "x-amzn-waf-action", "etag", "last-modified"}
        }
        try:
            body = await response.body()
        except PlaywrightError:
            if response is not self.response:
                return None
            raise
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("Browser response exceeds the 25 MiB limit")
        if response is not self.response:
            return None
        self.snapshot = {
            "url": response.url,
            "status": response.status,
            "headers": headers,
            "body": body,
        }
        return self.snapshot

    @staticmethod
    def _blocked(snapshot):
        return snapshot is None or is_challenge(snapshot["headers"], snapshot["body"])

    async def _try_solver(self, snapshot, deadline):
        if self.solver is None or self._attempted_episode == self.episode:
            return False
        self._attempted_episode = self.episode
        remaining = deadline - asyncio.get_running_loop().time() - 5
        if remaining <= 0:
            return False
        logger.info("Automatic CAPTCHA solve started; episode=%s", self.episode)
        try:
            async with asyncio.timeout(min(30, remaining)):
                await self.solver.solve(self.page, snapshot)
            await self.page.reload(wait_until="domcontentloaded", timeout=5000)
            return True
        except Exception as exc:  # noqa: BLE001 - solver boundary must preserve human handoff
            # Third-party failures must preserve the human handoff. Exception bodies
            # can contain model responses or AWS tokens, so log only their type.
            logger.warning(
                "Automatic CAPTCHA solve failed; episode=%s error=%s; handing off to human.",
                self.episode,
                type(exc).__name__,
            )
            return False

    async def fetch(self, url, headers=None):
        if not allowed_url(url):
            raise ValueError("Only OpenRent HTTPS URLs are allowed")
        headers = headers or {}
        if any(k.lower() not in {"x-requested-with", "accept"} for k in headers):
            raise ValueError("Unsupported request headers")
        async with self.lock:
            self.last_used = time.monotonic()
            try:
                async with asyncio.timeout(self.config.request_timeout) as request_deadline:
                    await self._ensure_browser()
                    # Polling must never reload a CAPTCHA during human interaction.
                    # Replay the requested URL after the challenge clears.
                    if self.episode and self.response and self._blocked(await self._snapshot()):
                        return {"episode": self.episode}
                    self.response = None
                    self.snapshot = None
                    await self.page.set_extra_http_headers(headers)
                    try:
                        await self.page.goto(url, wait_until="domcontentloaded")
                    except PlaywrightError:
                        # WAF JavaScript can replace the document during goto().
                        if not self.response:
                            raise
                    deadline = time.monotonic() + self.config.settle_seconds
                    while True:
                        snapshot = await self._snapshot()
                        if not self._blocked(snapshot):
                            if self.episode:
                                logger.info(
                                    "Browser verification cleared; request resumed; episode=%s url=%s status=%s",
                                    self.episode,
                                    safe_url(url),
                                    snapshot["status"],
                                )
                            self.episode = None
                            return snapshot
                        if time.monotonic() >= deadline:
                            if snapshot is None:
                                raise RuntimeError("Browser did not produce a readable response")
                            self.episode = self.episode or str(uuid4())
                            if await self._try_solver(snapshot, request_deadline.when()):
                                continue
                            logger.warning(
                                "Browser waiting for human verification; episode=%s url=%s",
                                self.episode,
                                safe_url(url),
                            )
                            return {"episode": self.episode}
                        await asyncio.sleep(0.5)
            finally:
                self.last_used = time.monotonic()


def application(worker, token):
    @web.middleware
    async def authenticate(request, handler):
        expected = f"Bearer {token}"
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise web.HTTPUnauthorized()
        return await handler(request)

    app = web.Application(middlewares=[authenticate], client_max_size=8192)

    async def fetch(request):
        try:
            data = await request.json()
            result = await worker.fetch(data["url"], data.get("headers"))
            if "episode" in result:
                return web.json_response(result, status=423)
            return web.json_response(
                result | {"body": base64.b64encode(result["body"]).decode("ascii")}
            )
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "Invalid request or oversized response"}, status=400)
        except (PlaywrightError, TimeoutError, OSError, RuntimeError) as exc:
            logger.error(
                "Browser operation failed: %s detail=%s", type(exc).__name__, safe_error(exc)
            )
            return web.json_response(
                {"error": "Browser unavailable; inspect worker logs"}, status=503
            )

    async def status(request):
        return web.json_response(
            {
                "state": "waiting_for_human"
                if worker.episode
                else "ready"
                if worker.context
                else "idle",
                "episode": worker.episode,
            }
        )

    async def lifecycle(app):
        await worker.start()
        yield
        await worker.close()

    app.router.add_post("/fetch", fetch)
    app.router.add_get("/status", status)
    app.cleanup_ctx.append(lifecycle)
    return app


def main():
    from .captcha_solver import configuration

    configure_logging()
    token = Path(os.environ["BROWSER_TOKEN_FILE"]).read_text().strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("Browser token must have at least 32 non-whitespace characters")
    worker = BrowserWorker(
        BrowserConfig(
            profile=Path(os.environ.get("BROWSER_PROFILE", "/state/chromium")),
            vnc_password_file=Path(os.environ["VNC_PASSWORD_FILE"]),
            idle_seconds=float(os.environ.get("BROWSER_IDLE_SECONDS", "300")),
            sandbox=os.environ.get("BROWSER_CHROMIUM_SANDBOX", "true").lower() != "false",
            proxy=os.environ.get("BROWSER_PROXY"),
        ),
        solver=configuration(os.environ),
    )
    web.run_app(application(worker, token), host="0.0.0.0", port=8091, access_log=None)


if __name__ == "__main__":
    main()
