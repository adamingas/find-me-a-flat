import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from dbos import DBOS
from pipeline_worker import property_fixture

from openrent import db as database
from openrent import ingestion, notifications
from openrent.models import Candidate, SearchData
from openrent.notifications import NotificationSettings, notify
from openrent.review import Assessment, ReviewConfig
from openrent.search import ApiFilters, WebsiteFilters


@pytest.mark.asyncio
async def test_image_steps_run_concurrently_and_review_waits_for_all(pipeline, monkeypatch):
    from archive_helpers import CONFIG
    from pipeline_worker import judgement

    from openrent import review

    prop = property_fixture()
    both_started, release = asyncio.Event(), asyncio.Event()
    started, assessed = [], []
    monkeypatch.setattr(ingestion, "parse_property", lambda *args: prop)

    async def image(url):
        started.append(url)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return url.encode(), "image/jpeg", (10, 10), {}

    async def judge(evidence, *args, **kwargs):
        assert len(evidence.images) == 2
        assert all(image.content for image in evidence.images)
        assessed.append(evidence.property_id)
        return judgement()

    monkeypatch.setattr(pipeline.client, "image", image)
    monkeypatch.setattr(review, "judge_property", judge)
    config = ingestion.DiscoveryConfig(ApiFilters(), WebsiteFilters(), review=CONFIG)
    handle = await DBOS.start_workflow_async(
        ingestion.download_property,
        Candidate(prop),
        config,
        SearchData([]),
        datetime.now(UTC).date(),
    )
    try:
        async with asyncio.timeout(10):
            await both_started.wait()
        assert assessed == [] and not await database.has_review(pipeline.db, prop.id)
    finally:
        release.set()
    assert await handle.get_result() == "downloaded"
    await (await DBOS.retrieve_workflow_async("review:1")).get_result()
    assert assessed == [prop.id]


@pytest.mark.asyncio
async def test_failed_image_waits_for_siblings_and_does_not_enqueue_review(pipeline, monkeypatch):
    from archive_helpers import CONFIG

    prop = property_fixture()
    completed = []
    monkeypatch.setattr(ingestion, "parse_property", lambda *args: prop)

    @DBOS.step()
    async def image_step(property_id, image):
        if image.position == 0:
            raise ValueError("image download failed")
        await asyncio.sleep(0.03)
        completed.append(image.position)

    monkeypatch.setattr(ingestion, "download_image", image_step)
    config = ingestion.DiscoveryConfig(ApiFilters(), WebsiteFilters(), review=CONFIG)
    handle = await DBOS.start_workflow_async(
        ingestion.download_property,
        Candidate(prop),
        config,
        SearchData([]),
        datetime.now(UTC).date(),
    )
    with pytest.raises(ValueError, match="image download failed"):
        await handle.get_result()
    assert completed == [1]
    assert not await database.has_review(pipeline.db, prop.id)
    assert await DBOS.get_workflow_status_async("review:1") is None


@pytest.mark.asyncio
async def test_missing_search_facts_and_post_filter_do_not_block_other_properties(
    pipeline, monkeypatch
):
    properties = [property_fixture(index) for index in range(1, 4)]
    properties[0].is_shared = None  # Unknown even after detail: this property fails explicitly.
    properties[1].epc_rating = "B"
    from openrent.models import NearbyPlace

    properties[1].nearby_places = [NearbyPlace("Station", "underground", walking_minutes=10)]
    properties[2].epc_rating = "D"  # Known post-filter rejection.
    candidates = [Candidate(prop) for prop in properties]
    monkeypatch.setattr(ingestion, "parse_search", lambda *args, **kwargs: SearchData(candidates))
    monkeypatch.setattr(
        ingestion,
        "parse_property",
        lambda text, url, candidate: properties[candidate.property.id - 1],
    )
    config = ingestion.DiscoveryConfig(
        ApiFilters(), WebsiteFilters(bedrooms_min=2), post_filter=True
    )
    handle = await DBOS.start_workflow_async(ingestion.discover, datetime.now(UTC), config)
    await handle.get_result()
    failed = await DBOS.retrieve_workflow_async("property:1")
    with pytest.raises(ValueError, match="missing is_shared"):
        await failed.get_result()
    passing = await DBOS.retrieve_workflow_async("property:2")
    assert await passing.get_result() == "downloaded"
    filtered = await DBOS.retrieve_workflow_async("property:3")
    assert await filtered.get_result() == "filtered"
    assert await database.get_property(pipeline.db, 1) is None
    assert await database.get_property(pipeline.db, 3) is None
    assert await database.get_property(pipeline.db, 2) is not None
    assert len(pipeline.client.downloaded) == 2
    # Rediscovery uses the existing property workflow rather than downloading it again.
    repeated = await DBOS.start_workflow_async(ingestion.discover, datetime.now(UTC), config)
    await repeated.get_result()
    assert len(pipeline.client.downloaded) == 2


@pytest.mark.asyncio
async def test_amenities_follow_cheap_filters_and_gate_images_and_review(pipeline, monkeypatch):
    from archive_helpers import CONFIG, gym_payload
    from pipeline_worker import FakeGrocerMetadataFetcher, judgement

    from openrent import review
    from openrent.gyms import GymAmenities

    properties = [property_fixture(index) for index in range(1, 5)]
    properties[0].bedrooms = 1
    fetched, reviewed, commutes = [], [], []
    payloads = {2: gym_payload(780), 3: gym_payload(900, 284), 4: gym_payload(284)}
    commute_payloads = {
        index: {"target": "Victoria", "duration_minutes": minutes}
        for index, minutes in ((2, 20), (3, 30), (4, 31))
    }

    class Fetcher:
        type = "gym"
        model = GymAmenities

        def __init__(self, client):
            pass

        async def fetch(self, property):
            assert await database.get_property(pipeline.db, property.id) is not None
            assert await database.query(
                pipeline.db,
                "SELECT count(*) AS n FROM property_images "
                "WHERE property_id = ? AND content_sha256 IS NOT NULL",
                (property.id,),
            ) == [{"n": 0}]
            fetched.append(property.id)
            return payloads[property.id]

    grocers = []

    class GrocerFetcher(FakeGrocerMetadataFetcher):
        async def fetch(self, property):
            assert fetched[-1] == property.id
            grocers.append(property.id)
            return await super().fetch(property)

    async def judge(evidence, *args, **kwargs):
        assert len(evidence.images) == 2
        assert all(image.content for image in evidence.images)
        assert evidence.data["amenities"] == {
            "gym": payloads[evidence.property_id],
            "grocer": {"grocers": [{"name": "Supermarket", "walking_seconds": 300}]},
            "commute": commute_payloads[evidence.property_id],
        }
        reviewed.append(evidence.property_id)
        return judgement()

    @DBOS.step()
    async def commute(property, target):
        commutes.append(property.id)
        await database.store_amenity(
            pipeline.db, property.id, "commute", json.dumps(commute_payloads[property.id])
        )

    monkeypatch.setattr(ingestion, "fetch_commute", commute)
    monkeypatch.setattr(ingestion, "GymMetadataFetcher", Fetcher)
    monkeypatch.setattr(ingestion, "GrocerMetadataFetcher", GrocerFetcher)
    monkeypatch.setattr(
        ingestion, "parse_property", lambda text, url, candidate: candidate.property
    )
    monkeypatch.setattr(review, "judge_property", judge)
    # The amenities rule runs even without the optional EPC/Tube post-filter.
    config = ingestion.DiscoveryConfig(ApiFilters(), WebsiteFilters(bedrooms_min=2), review=CONFIG)
    outcomes = []
    for prop in properties:
        handle = await DBOS.start_workflow_async(
            ingestion.download_property,
            Candidate(prop),
            config,
            SearchData([]),
            datetime.now(UTC).date(),
        )
        outcomes.append(await handle.get_result())
    await (await DBOS.retrieve_workflow_async("review:3")).get_result()
    assert outcomes == ["filtered", "filtered", "downloaded", "filtered"]
    assert fetched == [2, 3, 4]
    assert grocers == [2, 3, 4]
    assert await database.get_property(pipeline.db, 1) is None
    assert await database.get_amenities(pipeline.db, 1) == {}
    assert await database.get_amenities(pipeline.db, 2) == {
        "gym": payloads[2],
        "grocer": {"grocers": [{"name": "Supermarket", "walking_seconds": 300}]},
        "commute": commute_payloads[2],
    }
    assert reviewed == [3]
    assert commutes == [2, 3, 4]
    assert len(pipeline.client.downloaded) == 2
    assert await database.query(
        pipeline.db, "SELECT property_id FROM property_images WHERE content_sha256 IS NOT NULL"
    ) == [{"property_id": 3}, {"property_id": 3}]
    assert await DBOS.get_workflow_status_async("review:2") is None
    assert await DBOS.get_workflow_status_async("review:4") is None


@pytest.mark.asyncio
async def test_notify_sends_prepared_pass_and_unknown_items_once(pipeline, monkeypatch):
    monkeypatch.setenv("RESEND_TOKEN", "offline-test-secret")
    sent = []

    class Sender:
        def __init__(self, config):
            assert config.api_token == "offline-test-secret"

        async def send(self, recipient, *args, **kwargs):
            sent.append((recipient, kwargs["idempotency_key"]))
            return SimpleNamespace(accepted=True, provider_message_id="offline-id")

    monkeypatch.setattr(notifications, "ResendEmailSender", Sender)
    config = ReviewConfig("Preferences", "a" * 64, "offline", 30)
    for property_id, decision in enumerate(("pass", "uncertain", "reject"), start=1):
        prop = property_fixture(property_id)
        await database.upsert_property(pipeline.db, prop)
        for image in prop.images:
            await database.store_image(
                pipeline.db, property_id, image, image.source_url.encode(), "image/jpeg"
            )
        evidence = await database.get_evidence(pipeline.db, property_id)
        await database.save_review(
            pipeline.db,
            config,
            Assessment(
                property_id,
                evidence.fingerprint,
                evidence.data,
                tuple(evidence.data["images"]),
                json.dumps({"decision": decision, "summary": decision}),
            ),
        )
    settings = NotificationSettings(
        pipeline.path, ("test@example.test",), None, 30, pipeline.path.parent / "absent.env"
    )
    assert "offline-test-secret" not in repr(asdict(settings))
    for _ in range(2):
        handle = await DBOS.start_workflow_async(notify, datetime.now(UTC), settings)
        await handle.get_result()
    assert len(sent) == 1
    assert await database.pending_notifications(pipeline.db, "test@example.test") == []
