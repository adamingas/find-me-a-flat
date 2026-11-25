"""Application-facing contract for document transports.

No browser library belongs at this boundary. Implementations return buffered
HTTP responses so the client retains parsing, status handling and image checks.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

import httpx

QueryParams = httpx.QueryParams | Mapping[str, str] | Sequence[tuple[str, str]]
RequestHeaders = Mapping[str, str]
BeforeRequest = Callable[[httpx.Request], Awaitable[None]]


class ResponseTooLarge(ValueError):
    """A transport stopped reading after the caller's response limit."""


class FetchTransport(Protocol):
    """Owned by OpenRentClient and closed when that client exits.

    Call before_request for each attempt, including challenge replays, inside
    any transport serialization lock. Cancellation must release that lock.
    Enforce max_bytes when supplied; streaming implementations should stop
    reading as soon as the limit is exceeded.
    """

    async def get(
        self,
        url: str,
        *,
        params: QueryParams | None = None,
        headers: RequestHeaders | None = None,
        before_request: BeforeRequest | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response: ...

    async def aclose(self) -> None: ...
