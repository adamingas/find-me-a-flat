"""Asynchronous requests to OpenRent and its public summary API."""

import asyncio
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
from tenacity import retry, retry_if_exception, stop_after_attempt

from .browser_protocol import is_challenge
from .fetch_transport import FetchTransport, ResponseTooLarge
from .http_logging import log_http_failure, safe_url
from .http_transport import HttpFetchTransport

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
        try:
            return await operation(self, url, *args, **kwargs)
        except httpx.HTTPStatusError as exc:
            log_http_failure(exc.response, "http_status_error")
            raise

    def log_retry(state):
        logging.getLogger(__name__).warning(
            "Request retry: url=%s failed_attempt=%s delay_seconds=%s error=%s",
            safe_url(state.args[1]),
            state.attempt_number,
            state.next_action.sleep,
            type(state.outcome.exception()).__name__,
        )

    retrying = retry(
        retry=retry_if_exception(
            lambda exc: (
                isinstance(exc, httpx.TransportError)
                or isinstance(exc, httpx.HTTPStatusError)
                and (exc.response.status_code == 429 or exc.response.status_code >= 500)
            )
        ),
        stop=stop_after_attempt(6),
        wait=retry_wait,
        sleep=lambda seconds: asyncio.sleep(seconds),
        before_sleep=log_retry,
        reraise=True,
    )(attempt)

    @wraps(operation)
    async def wrapped(self, url, *args, **kwargs):
        try:
            return await retrying(self, url, *args, **kwargs)
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            status = response.status_code if response is not None else "no response"
            raise FetchError(
                f"Could not fetch {safe_url(url)}: {type(exc).__name__}; status={status}"
            ) from None

    return wrapped


class OpenRentClient:
    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 1,
        requests_per_second: float = 0.2,
        cookie_file: Path | None = None,
        *,
        transport: FetchTransport | None = None,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive.")
        if concurrency < 1:
            raise ValueError("Concurrency must be positive.")
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("Requests per second must be positive and finite.")
        self._rate_lock = asyncio.Lock()
        self._interval = 1 / requests_per_second
        self._next_request_at = 0.0
        if transport is not None and cookie_file is not None:
            raise ValueError("Configure cookie_file on the injected HTTP transport.")
        try:
            self.transport = (
                transport
                if transport is not None
                else HttpFetchTransport(
                    timeout=timeout, cookie_file=cookie_file, concurrency=concurrency
                )
            )
        except ValueError as exc:
            raise FetchError(str(exc)) from exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.transport.aclose()

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
            raise FetchError(f"Refusing a non-OpenRent HTTPS source URL: {safe_url(url)}")

    @staticmethod
    def _check_access(response):
        if is_challenge(response.headers):
            raise FetchError(
                "OpenRent requires browser verification; configure --browser-endpoint "
                "and complete verification through its noVNC viewer."
            )
        if response.status_code in (403, 401):
            log_http_failure(response, "access_refused")
            raise FetchError(
                f"OpenRent refused access ({response.status_code}). "
                "If needed, supply your own exported session with --cookie-file."
            )

    @retry_request
    async def get(self, url, params=None, headers=None) -> httpx.Response:
        self._check_url(url)
        response = await self.transport.get(
            url, params=params, headers=headers, before_request=self._on_request
        )
        self._check_access(response)
        if is_challenge(response.headers, response.content):
            raise FetchError("OpenRent returned a browser challenge instead of listing data.")
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
            log_http_failure(response, "invalid_summary_json")
            raise FetchError("OpenRent summary endpoint returned invalid JSON.") from exc
        if not isinstance(summaries, list) or not all(
            isinstance(item, dict) and "id" in item for item in summaries
        ):
            log_http_failure(response, "invalid_summary_format")
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
            raise FetchError(f"Source returned an invalid image: {safe_url(url)}") from exc
        return mime, dimensions

    @retry_request
    async def image(self, url):
        """Stream and verify photos, rejecting error pages and oversized downloads."""
        self._check_url(url)
        try:
            response = await self.transport.get(
                url, before_request=self._on_request, max_bytes=MAX_IMAGE_BYTES
            )
        except ResponseTooLarge as exc:
            raise FetchError("Image exceeds the 25 MiB download limit.") from exc
        self._check_access(response)
        response.raise_for_status()
        content = response.content
        if len(content) > MAX_IMAGE_BYTES:
            raise FetchError("Image exceeds the 25 MiB download limit.")
        mime, dimensions = await asyncio.to_thread(self._verify_image, content, url)
        return content, mime, dimensions, response.headers
