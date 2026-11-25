"""Asynchronous HTTP implementation of the application's fetch contract."""

import http.cookiejar
from contextvars import ContextVar
from pathlib import Path

import httpx

from .fetch_transport import (
    BeforeRequest,
    FetchTransport,
    QueryParams,
    RequestHeaders,
    ResponseTooLarge,
)


class HttpFetchTransport(FetchTransport):
    def __init__(
        self,
        *,
        timeout: float = 30,
        cookie_file: Path | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        cookies = None
        if cookie_file:
            cookies = http.cookiejar.MozillaCookieJar(str(cookie_file))
            try:
                cookies.load(ignore_discard=True, ignore_expires=False)
            except (OSError, http.cookiejar.LoadError) as exc:
                raise ValueError(f"Cannot read Netscape cookie file: {exc}") from exc
        # Hooks apply to redirects too. Context-local callbacks keep concurrent
        # fetches independent without mutating the shared client's event hooks.
        self._before_request: ContextVar[BeforeRequest | None] = ContextVar(
            "before_request", default=None
        )
        self.http = httpx.AsyncClient(
            transport=http_transport,
            timeout=timeout,
            cookies=cookies,
            follow_redirects=True,
            event_hooks={"request": [self._on_request]},
            headers={
                "User-Agent": "openrent-fetch/0.1 (personal rental search)",
                "Referer": "https://www.openrent.co.uk/",
                "Accept": "text/html,application/json,image/*;q=0.9,*/*;q=0.5",
            },
        )

    async def _on_request(self, request: httpx.Request) -> None:
        callback = self._before_request.get()
        if callback is not None:
            await callback(request)

    async def get(
        self,
        url: str,
        *,
        params: QueryParams | None = None,
        headers: RequestHeaders | None = None,
        before_request: BeforeRequest | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        token = self._before_request.set(before_request)
        try:
            async with self.http.stream("GET", url, params=params, headers=headers) as response:
                if max_bytes is None:
                    await response.aread()
                    return response
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                # aiter_bytes already decoded compression. Preserve the response
                # metadata without asking HTTPX to decode the buffered bytes twice.
                decoded_headers = [
                    (key, value)
                    for key, value in response.headers.multi_items()
                    if key.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                ]
                return httpx.Response(
                    response.status_code,
                    headers=decoded_headers,
                    content=b"".join(chunks),
                    request=response.request,
                    history=response.history,
                    extensions=response.extensions,
                )
        finally:
            self._before_request.reset(token)

    async def aclose(self) -> None:
        await self.http.aclose()
