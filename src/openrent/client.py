"""Asynchronous requests to OpenRent and its public summary API."""

import asyncio
import http.cookiejar
import io
import logging
import math
import time
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image as PILImage
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt

BASE_URL = "https://www.openrent.co.uk"
MAX_IMAGE_BYTES = 25 * 1024 * 1024


class FetchError(RuntimeError):
    """A page or photo could not be fetched without losing integrity."""


def retry_wait(state):
    response = getattr(state.outcome.exception(), "response", None)
    if response is not None and response.headers.get("Retry-After"):
        value = response.headers["Retry-After"]
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = float("nan")
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    limited = response is not None and response.status_code == 429
    return min((30 if limited else 1) * 2 ** (state.attempt_number - 1), 300 if limited else 30)


def retry_request(operation):
    @wraps(operation)
    async def attempt(self, url, *args, **kwargs):
        async with self._slots:
            return await operation(self, url, *args, **kwargs)

    retrying = retry(
        retry=retry_if_exception(
            lambda exc: isinstance(exc, httpx.TransportError)
            or isinstance(exc, httpx.HTTPStatusError)
            and (exc.response.status_code == 429 or exc.response.status_code >= 500)
        ),
        stop=stop_after_attempt(6),
        wait=retry_wait,
        sleep=lambda seconds: asyncio.sleep(seconds),
        before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),
        reraise=True,
    )(attempt)

    @wraps(operation)
    async def wrapped(self, url, *args, **kwargs):
        try:
            return await retrying(self, url, *args, **kwargs)
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            detail = ""
            if response is not None and response.status_code == 405 and response.is_stream_consumed:
                detail = f"\n405 response (Allow: {response.headers.get('Allow', 'missing')}): {response.text[:2000]}"
            raise FetchError(f"Could not fetch {url}: {exc}{detail}") from exc

    return wrapped


class OpenRentClient:
    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 1,
        requests_per_second: float = 0.2,
        cookie_file: Path | None = None,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive.")
        if concurrency < 1:
            raise ValueError("Concurrency must be positive.")
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("Requests per second must be positive and finite.")
        self._slots = asyncio.Semaphore(concurrency)
        self._rate_lock = asyncio.Lock()
        self._interval = 1 / requests_per_second
        self._next_request_at = 0.0
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
        # Count every HTTP attempt, including redirects and retries.
        self._check_url(str(request.url))
        async with self._rate_lock:
            wait = self._next_request_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request_at = time.monotonic() + self._interval

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

    @retry_request
    async def get(self, url, params=None, headers=None) -> httpx.Response:
        self._check_url(url)
        response = await self.http.get(url, params=params, headers=headers)
        self._check_access(response)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            await response.aclose()
            raise
        return response

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

    @retry_request
    async def image(self, url):
        """Stream and verify photos, rejecting error pages and oversized downloads."""
        self._check_url(url)
        async with self.http.stream("GET", url) as response:
            self._check_access(response)
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise FetchError("Image exceeds the 25 MiB download limit.")
                chunks.append(chunk)
            content = b"".join(chunks)
            headers = response.headers
        mime, dimensions = await asyncio.to_thread(self._verify_image, content, url)
        return content, mime, dimensions, headers
