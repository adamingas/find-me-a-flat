"""Tests use fake clients or explicit HTTPX mock transports, never live services."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    async def forbidden_async(self, request):
        raise AssertionError(f"Unexpected live HTTP request: {request.method} {request.url}")

    def forbidden_sync(self, request):
        raise AssertionError(f"Unexpected live HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync)
