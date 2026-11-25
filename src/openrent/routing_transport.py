"""Compose HTTP and browser fetching using an explicit application policy."""

from collections.abc import Callable

import httpx

from .fetch_transport import BeforeRequest, FetchTransport, QueryParams, RequestHeaders


class RoutingFetchTransport(FetchTransport):
    """Select once per logical request; the selected transport owns its attempts.

    Both children are owned and closed by this router. Session state remains in
    each child; routing does not transfer cookies or infer browser clearance.
    """

    def __init__(
        self,
        http: FetchTransport,
        browser: FetchTransport,
        use_browser: Callable[[httpx.Request], bool],
    ):
        self.http = http
        self.browser = browser
        self.use_browser = use_browser

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
        selected = self.browser if self.use_browser(request) else self.http
        return await selected.get(
            url,
            params=params,
            headers=headers,
            before_request=before_request,
            max_bytes=max_bytes,
        )

    async def aclose(self) -> None:
        try:
            await self.http.aclose()
        finally:
            if self.browser is not self.http:
                await self.browser.aclose()
