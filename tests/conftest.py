"""Tests use fake clients or explicit HTTPX mock transports, never live services."""

import asyncio
from types import SimpleNamespace

import httpx
import httpx2
import pytest
import pytest_asyncio
from dbos import DBOS
from pipeline_worker import FakeGrocerMetadataFetcher, FakeGymMetadataFetcher

from openrent import db as database
from openrent import ingestion
from openrent.daemon import register_queues
from openrent.runtime import Runtime, configure


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    async def forbidden_async(self, request):
        raise AssertionError(f"Unexpected live HTTP request: {request.method} {request.url}")

    def forbidden_sync(self, request):
        raise AssertionError(f"Unexpected live HTTP request: {request.method} {request.url}")

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", forbidden_async)
        monkeypatch.setattr(module.HTTPTransport, "handle_request", forbidden_sync)


@pytest.fixture
def mock_email_transport(monkeypatch):
    """Exercise the real sender's client lifetime with an in-process provider."""
    client_type = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            "openrent.email_sender.httpx.AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
        )

    return install


@pytest_asyncio.fixture
async def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(ingestion, "GymMetadataFetcher", FakeGymMetadataFetcher)
    monkeypatch.setattr(ingestion, "GrocerMetadataFetcher", FakeGrocerMetadataFetcher)

    class Client:
        def __init__(self):
            self.downloaded = []

        async def search(self, parameters):
            return SimpleNamespace(text="offline", url="https://example.test/search")

        async def summaries(self, ids):
            return []

        async def get(self, url):
            return SimpleNamespace(text="offline", url=url)

        async def image(self, url):
            self.downloaded.append(url)
            return url.encode(), "image/jpeg", (10, 10), {}

    async with database.connect(tmp_path / "archive.sqlite") as db:
        client = Client()
        configure(Runtime(client, db, quiet=True))
        DBOS(
            config={
                "name": "openrent-test",
                "system_database_url": f"sqlite:///{tmp_path / 'system.sqlite'}",
                "log_level": "ERROR",
            }
        )
        DBOS.launch()
        await register_queues(2, 1)
        try:
            yield SimpleNamespace(db=db, client=client, path=tmp_path / "archive.sqlite")
        finally:
            await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=1)
            configure(None)
