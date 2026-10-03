"""Bounded asynchronous requests to OpenRent and its public summary API."""

import asyncio
import http.cookiejar
import io
import math
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image as PILImage

BASE_URL = "https://www.openrent.co.uk"
MAX_IMAGE_BYTES = 25 * 1024 * 1024


class FetchError(RuntimeError):
    """A page or photo could not be fetched without losing integrity."""


class OpenRentClient:
    def __init__(
        self,
        delay: float = 0.5,
        timeout: float = 30.0,
        retries: int = 3,
        cookie_file: Path | None = None,
        concurrency: int = 4,
    ):
        if (
            not math.isfinite(delay)
            or delay < 0
            or not isinstance(retries, int)
            or retries < 0
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise ValueError("Delay and retries must be nonnegative; concurrency must be positive.")
        self.delay, self.retries = delay, retries
        self._last_request = 0.0
        self._cooldown_until = 0.0
        self._server_pause_error = None
        self._pace_lock = asyncio.Lock()
        self._requests = asyncio.Semaphore(concurrency)
        cookies = None
        if cookie_file:
            cookies = http.cookiejar.MozillaCookieJar(str(cookie_file))
            try:
                cookies.load(ignore_discard=True, ignore_expires=False)
            except (OSError, http.cookiejar.LoadError) as exc:
                raise FetchError(f"Cannot read Netscape cookie file: {exc}") from exc
        self.http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            cookies=cookies,
            event_hooks={"request": [self._on_request]},
            headers={
                "User-Agent": "openrent-fetch/0.1 (personal rental search)",
                "Referer": BASE_URL + "/",
                "Accept": "text/html,application/json,image/*;q=0.9,*/*;q=0.5",
            },
        )

    async def __aenter__(self):
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.http.__aexit__(*args)

    async def _on_request(self, request):
        # HTTPX also invokes request hooks for redirect hops. The semaphore still
        # covers the whole attempt, while each actual request reserves a start.
        self._check_url(str(request.url))
        await self._pace()

    async def _pace(self):
        """Reserve a request start, sharing its interval and server cooldown."""
        async with self._pace_lock:
            while True:
                if self._server_pause_error:
                    raise FetchError(self._server_pause_error)
                now = time.monotonic()
                wait = max(self._last_request + self.delay, self._cooldown_until) - now
                if wait <= 0:
                    self._last_request = now
                    return
                # Recheck after sleeping: a different response can extend the cooldown.
                await asyncio.sleep(wait)

    def _retry_wait(self, attempt, response=None):
        wait = min(2**attempt, 30)
        retry_after = None
        if response is not None and response.headers.get("Retry-After"):
            value = response.headers["Retry-After"]
            try:
                retry_after = float(value)
            except ValueError:
                try:
                    retry_after = parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    pass
            if retry_after is not None and math.isfinite(retry_after):
                wait = max(wait, retry_after)
            else:
                retry_after = None
        if wait > 60:
            self._server_pause_error = (
                f"Server requested a {wait:.0f}s pause. Retry the command later."
            )
            raise FetchError(self._server_pause_error)
        if retry_after is not None:
            # No await between reading and writing: other tasks observe the cooldown
            # immediately, including a task already sleeping inside _pace.
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait)
        return wait

    async def _backoff(self, attempt, response=None):
        wait = self._retry_wait(attempt, response)
        await asyncio.sleep(wait)

    @staticmethod
    def _check_url(url):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host == "openrent.co.uk" or host.endswith(".openrent.co.uk")
        ):
            raise FetchError(f"Refusing a non-OpenRent HTTPS source URL: {url}")

    @staticmethod
    def _check_access(response):
        if response.status_code in (403, 401):
            raise FetchError(
                f"OpenRent refused access ({response.status_code}). "
                "If needed, supply your own exported session with --cookie-file."
            )

    @staticmethod
    def _retryable(exc):
        return isinstance(exc, httpx.TransportError) or (
            exc.response.status_code == 429 or exc.response.status_code >= 500
        )

    async def get(self, url, params=None, headers=None) -> httpx.Response:
        self._check_url(url)
        for attempt in range(self.retries + 1):
            response = None
            try:
                async with self._requests:
                    response = await self.http.get(url, params=params, headers=headers)
                    self._check_access(response)
                    if (response.status_code == 429 or response.status_code >= 500) and (
                        response.headers.get("Retry-After")
                    ):
                        self._retry_wait(attempt, response)
                    response.raise_for_status()
                    return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if self._retryable(exc) and attempt < self.retries:
                    if response is not None:
                        await response.aclose()
                    # The semaphore is released before any retry delay.
                    await self._backoff(attempt, response)
                    continue
                raise FetchError(f"Could not fetch {url}: {exc}") from exc
        raise FetchError(f"Request failed: {url}")

    async def search(self, params):
        return await self.get(BASE_URL + "/search/search_bycommutetime", params=params)

    async def summaries(self, ids: list[int]) -> list[dict]:
        if len(ids) > 20:
            raise ValueError("OpenRent summary batches must contain at most 20 IDs.")
        if not ids:
            return []
        response = await self.get(
            BASE_URL + "/search/propertiesbyid",
            params=[("ids", str(i)) for i in ids],
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            summaries = response.json()
        except ValueError as exc:
            raise FetchError("OpenRent summary endpoint returned invalid JSON.") from exc
        if not isinstance(summaries, list) or not all(
            isinstance(item, dict) and "id" in item for item in summaries
        ):
            raise FetchError("OpenRent summary endpoint changed its response format.")
        return summaries

    @staticmethod
    def _verify_image(content, url):
        try:
            with PILImage.open(io.BytesIO(content)) as picture:
                dimensions = picture.size
                mime = PILImage.MIME.get(picture.format, "application/octet-stream")
                picture.verify()
        except (OSError, ValueError, PILImage.DecompressionBombError) as exc:
            raise FetchError(f"Source returned an invalid image: {url}") from exc
        return mime, dimensions

    async def image(self, url):
        """Stream and verify photos, rejecting error pages and oversized downloads."""
        self._check_url(url)
        for attempt in range(self.retries + 1):
            response = None
            try:
                async with self._requests, self.http.stream("GET", url) as response:
                    self._check_access(response)
                    if (response.status_code == 429 or response.status_code >= 500) and (
                        response.headers.get("Retry-After")
                    ):
                        # Publish before stream cleanup can yield to another worker.
                        self._retry_wait(attempt, response)
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            raise FetchError("Image exceeds the 25 MiB download limit.")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    headers = response.headers
                # Pillow verification runs off the event loop, after closing the stream.
                mime, dimensions = await asyncio.to_thread(self._verify_image, content, url)
                return content, mime, dimensions, headers
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if self._retryable(exc) and attempt < self.retries:
                    await self._backoff(attempt, response)
                    continue
                raise FetchError(f"Could not download image {url}: {exc}") from exc
        raise FetchError(f"Image download failed: {url}")
