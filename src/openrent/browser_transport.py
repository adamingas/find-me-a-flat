"""Authenticated browser RPC and a cancellable human handoff."""

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable

import httpx

from .browser_protocol import MAX_BODY_BYTES, allowed_url
from .fetch_transport import (
    BeforeRequest,
    FetchTransport,
    QueryParams,
    RequestHeaders,
    ResponseTooLarge,
)

logger = logging.getLogger(__name__)


class BrowserTransport(FetchTransport):
    def __init__(
        self,
        endpoint,
        token,
        notify: Callable[[str], Awaitable[None]],
        *,
        poll_seconds=10,
        timeout=90,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.notify = notify
        self.poll_seconds = poll_seconds
        self._lock = asyncio.Lock()
        self.http = httpx.AsyncClient(
            transport=http_transport,
            base_url=endpoint.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

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
        async with self._lock:
            while True:
                if before_request:
                    # Pace inside the shared browser lock, including challenge replays.
                    await before_request(request)
                response = await self.http.post(
                    "fetch", json={"url": str(request.url), "headers": dict(headers or {})}
                )
                if response.status_code == 423:
                    episode = response.json()["episode"]
                    try:
                        await self.notify(episode)
                    except (OSError, ValueError) as exc:
                        logger.error("Browser alert unavailable (%s).", type(exc).__name__)
                    # Cancellation releases this lock immediately on daemon shutdown.
                    await asyncio.sleep(self.poll_seconds)
                    continue
                response.raise_for_status()
                result = response.json()
                if not allowed_url(result["url"]):
                    raise ValueError("Browser returned a non-OpenRent URL.")
                encoded = result["body"]
                if len(encoded) > 4 * ((MAX_BODY_BYTES + 2) // 3):
                    raise ValueError("Browser response exceeds the 25 MiB limit.")
                content = base64.b64decode(encoded, validate=True)
                if len(content) > MAX_BODY_BYTES:
                    raise ValueError("Browser response exceeds the 25 MiB limit.")
                if max_bytes is not None and len(content) > max_bytes:
                    raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
                return httpx.Response(
                    result["status"],
                    headers=result["headers"],
                    content=content,
                    request=httpx.Request("GET", result["url"]),
                )
