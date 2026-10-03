"""Exercise the real CLI and SQLite writer across repeat and interrupted imports."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from openrent import cli
from openrent.client import FetchError
from openrent.db import Database
from openrent.models import Candidate, Image, Property, SearchData


@pytest.fixture
def fake_source(monkeypatch):
    state = SimpleNamespace(
        image_calls=0,
        detail_fails=False,
        image_fails=False,
        api_fails=False,
        io_delay=0,
        active_details=0,
        peak_details=0,
        active_images=0,
        peak_images=0,
        open_clients=0,
        block_images=False,
    )
    listing = Property(
        id=101,
        url="https://www.openrent.co.uk/101",
        title="1 Bed Flat, Example, CB1",
        latitude=52.2,
        longitude=0.12,
        bedrooms=1,
        bathrooms=1,
        is_live=True,
        rent_pcm_pence=125000,
        detail_complete=True,
        images=[Image("https://imagescdn.openrent.co.uk/101/a.png", width=7, height=9)],
    )
    search = SearchData(
        [Candidate(listing, distance_km=0)],
        latitude=52.2,
        longitude=0.12,
        location="Cambridge",
        distance_unit="km",
        total=1,
    )
    state.search = search
    state.summary_batches = []
    state.detail_ids = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            state.open_clients += 1
            return self

        async def __aexit__(self, *args):
            state.open_clients -= 1

        async def search(self, params):
            return SimpleNamespace(text="search", url="https://www.openrent.co.uk/search")

        async def summaries(self, ids):
            state.summary_batches.append(ids)
            if state.api_fails:
                raise FetchError("summary temporary failure")
            return [{"id": property_id} for property_id in ids]

        async def get(self, url):
            state.detail_ids.append(int(url.rsplit("/", 1)[1]))
            state.active_details += 1
            state.peak_details = max(state.peak_details, state.active_details)
            try:
                await asyncio.sleep(state.io_delay)
                if state.detail_fails:
                    raise FetchError("detail temporary failure")
                return SimpleNamespace(text="detail", url=url)
            finally:
                state.active_details -= 1

        async def image(self, url):
            state.image_calls += 1
            state.active_images += 1
            state.peak_images = max(state.peak_images, state.active_images)
            try:
                if state.block_images:
                    state.images_started.set()
                    await asyncio.Event().wait()
                await asyncio.sleep(state.io_delay)
                if state.image_fails:
                    raise FetchError("photo temporary failure")
                return b"image-data", "image/png", (7, 9), {}
            finally:
                state.active_images -= 1

    monkeypatch.setattr(cli, "OpenRentClient", FakeClient)
    monkeypatch.setattr(cli, "parse_search", lambda *args, **kwargs: copy.deepcopy(state.search))
    monkeypatch.setattr(cli, "parse_property", lambda html, url, candidate: candidate.property)
    monkeypatch.setattr(cli, "enrich_summary", lambda candidate, summary: candidate)
    return state


def run_import(path, *flags):
    return cli.main(
        [
            "fetch",
            "--location",
            "Cambridge",
            "--radius-distance",
            "1",
            "--db",
            str(path),
            "--quiet",
            *flags,
        ]
    )


def test_two_cli_runs_do_not_append_rows_or_redownload(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    assert run_import(path) == 0
    with Database(path) as db:
        first_counts = db.counts()
        updated_at = db.get_property(101)["updated_at"]
    assert run_import(path) == 0
    with Database(path) as db:
        assert db.counts() == first_counts
        assert db.get_property(101)["updated_at"] == updated_at
    assert fake_source.image_calls == 1


def test_all_matches_across_multiple_summary_batches_are_imported(fake_source, tmp_path):
    original = fake_source.search.candidates[0]
    candidates = []
    for property_id in range(101, 148):
        candidate = copy.deepcopy(original)
        candidate.property.id = property_id
        candidate.property.url = f"https://www.openrent.co.uk/{property_id}"
        candidates.append(candidate)
    fake_source.search.candidates = candidates
    fake_source.search.total = len(candidates)
    path = tmp_path / "archive.sqlite"

    assert run_import(path, "--skip-images") == 0
    assert [len(batch) for batch in fake_source.summary_batches] == [20, 20, 7]
    assert sorted(fake_source.detail_ids) == list(range(101, 148))
    with Database(path) as db:
        first_counts = db.counts()
        assert first_counts["properties"] == 47
        assert db.connection.execute("SELECT COUNT(*) FROM search_matches").fetchone()[0] == 47
        assert db.connection.execute("SELECT last_search_complete FROM searches").fetchone()[0] == 1

    assert run_import(path, "--skip-images") == 0
    with Database(path) as db:
        assert db.counts() == first_counts


def test_details_and_photos_are_concurrent_and_bounded(fake_source, tmp_path):
    original = fake_source.search.candidates[0]
    candidates = []
    for property_id in range(101, 107):
        candidate = copy.deepcopy(original)
        candidate.property.id = property_id
        candidate.property.url = f"https://www.openrent.co.uk/{property_id}"
        candidate.property.images = [
            Image(f"https://imagescdn.openrent.co.uk/{property_id}/{position}.png", position)
            for position in range(3)
        ]
        candidates.append(candidate)
    fake_source.search.candidates = candidates
    fake_source.search.total = len(candidates)
    fake_source.io_delay = 0.005
    path = tmp_path / "archive.sqlite"

    assert run_import(path, "--concurrency", "3") == 0
    assert fake_source.peak_details == 3
    assert fake_source.peak_images == 3
    assert fake_source.active_details == fake_source.active_images == fake_source.open_clients == 0
    with Database(path) as db:
        first_counts = db.counts()
        assert first_counts["properties"] == 6
        assert first_counts["downloaded_images"] == 18
        assert first_counts["image_blobs"] == 1
    assert run_import(path, "--concurrency", "3") == 0
    assert fake_source.image_calls == 18
    with Database(path) as db:
        assert db.counts() == first_counts


def test_cancellation_closes_workers_and_preserves_resumable_import(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"

    async def cancel_scan():
        fake_source.block_images = True
        fake_source.images_started = asyncio.Event()
        with cli.app.commands["fetch"].make_context(
            "fetch", ["--location", "Cambridge", "--radius-distance", "1", "--db", str(path)]
        ) as context:
            args = SimpleNamespace(**context.params)
        task = asyncio.create_task(cli.fetch(args))
        await asyncio.wait_for(fake_source.images_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert fake_source.active_images == fake_source.active_details == 0
        assert fake_source.open_clients == 0

    asyncio.run(cancel_scan())
    with Database(path) as db:
        assert db.counts()["properties"] == 1
        assert db.counts()["pending_images"] == 1
        assert db.connection.execute("SELECT last_search_complete FROM searches").fetchone()[0] == 0
    fake_source.block_images = False
    assert run_import(path) == 0
    with Database(path) as db:
        assert db.counts()["properties"] == 1
        assert db.counts()["downloaded_images"] == 1
        assert db.connection.execute("SELECT last_search_complete FROM searches").fetchone()[0] == 1


def test_database_failure_cancels_download_workers_and_returns_error(
    fake_source, tmp_path, monkeypatch
):
    import sqlite3

    async def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    monkeypatch.setattr(cli.AsyncDatabase, "store_image", fail)
    assert run_import(tmp_path / "archive.sqlite") == 1
    assert fake_source.active_images == fake_source.active_details == fake_source.open_clients == 0


def test_failed_photo_is_retried_on_next_run(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    fake_source.image_fails = True
    assert run_import(path) == 1
    with Database(path) as db:
        assert db.counts()["failed_images"] == 1
    fake_source.image_fails = False
    assert run_import(path) == 0
    with Database(path) as db:
        assert db.counts()["properties"] == 1
        assert db.counts()["downloaded_images"] == 1
        assert db.counts()["failed_images"] == 0


def test_detail_failure_does_not_deactivate_previous_match(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    assert run_import(path) == 0
    fake_source.detail_fails = True
    assert run_import(path) == 1
    with Database(path) as db:
        assert db.connection.execute("SELECT active FROM search_matches").fetchone()[0] == 1
        assert db.connection.execute("SELECT last_search_complete FROM searches").fetchone()[0] == 0


def test_missing_requested_filter_data_cannot_become_a_successful_empty_scan(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    fake_source.search.candidates[0].property.pets_allowed = True
    assert run_import(path, "--pets", "--skip-images") == 0
    fake_source.search.candidates[0].property.pets_allowed = None

    assert run_import(path, "--pets", "--skip-images") == 1
    with Database(path) as db:
        assert db.counts()["properties"] == 1
        assert db.connection.execute("SELECT active FROM search_matches").fetchone()[0] == 1
    # The failed scan stops before detail requests and match reconciliation.
    assert fake_source.detail_ids == [101]


def test_summary_api_failure_can_fall_back_to_detail(fake_source, tmp_path):
    fake_source.api_fails = True
    assert run_import(tmp_path / "archive.sqlite") == 0


def test_skip_images_preserves_metadata_and_later_downloads(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    assert run_import(path, "--skip-images") == 0
    with Database(path) as db:
        assert db.counts()["pending_images"] == 1
    assert fake_source.image_calls == 0
    assert run_import(path) == 0
    assert fake_source.image_calls == 1


def test_dry_run_does_not_create_database(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    assert run_import(path, "--dry-run") == 0
    assert not path.exists()


def test_non_london_commute_is_rejected_without_database(fake_source, tmp_path):
    path = tmp_path / "archive.sqlite"
    assert (
        cli.main(
            [
                "fetch",
                "--location",
                "Cambridge",
                "--radius-minutes",
                "15",
                "--db",
                str(path),
                "--quiet",
            ]
        )
        == 1
    )
    assert not path.exists()
