"""Digest retries, per-recipient accounting, previews and runtime credentials."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from archive_helpers import add_listing, gym_payload, saved_review
from bs4 import BeautifulSoup

from openrent import db as database
from openrent import notifications


def notify_args(path, *recipients, **kwargs):
    values = {
        "db": path,
        "email_to": recipients,
        "email_preview": None,
        "env_file": None,
        "email_timeout": 1,
    }
    return SimpleNamespace(**(values | kwargs))


async def deliver(args):
    config = notifications.configuration(args)
    async with database.connect(args.db) as db:
        return await notifications.send_pending(db, config, lambda message: None)


def accepted(recipient):
    return httpx.Response(200, json={"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"})


@pytest.mark.asyncio
async def test_recipient_isolation_frozen_retry_and_unknown_delivery_are_not_resent(
    tmp_path, monkeypatch, mock_email_transport
):
    monkeypatch.setenv("RESEND_TOKEN", "test-placeholder")
    path = tmp_path / "archive.sqlite"
    for property_id, decision in ((1, "pass"), (2, "reject"), (3, "uncertain")):
        await saved_review(path, property_id, decision)
    first, second = "first@example.com", "second@example.com"
    timeout_recipient, cancelled_recipient = "timeout@example.com", "cancelled@example.com"
    phase = "reject"
    requests = []
    cancellation_started = asyncio.Event()

    async def post(request):
        payload = json.loads(request.content)
        requests.append(payload)
        recipient = payload["to"][0]
        assert "https://www.openrent.co.uk/property-to-rent/london/2" not in payload["html"]
        if recipient == first and phase == "reject":
            return httpx.Response(400, json={"success": False})
        if recipient == timeout_recipient:
            raise httpx.ReadTimeout("mocked receipt timeout", request=request)
        if recipient == cancelled_recipient:
            cancellation_started.set()
            await asyncio.Future()
        return accepted(recipient)

    async def run():
        nonlocal phase
        mock_email_transport(post)
        assert await deliver(notify_args(path, first.upper(), second)) == 1
        assert [item["to"] for item in requests] == [[first], [second]]
        assert requests[0]["html"] == requests[1]["html"]
        assert requests[0]["subject"] == "2 new flats found"
        assert "https://www.openrent.co.uk/property-to-rent/london/3" in requests[0]["html"]
        async with database.connect(path) as db:
            assert (await database.fetch_all(db, "SELECT status FROM email_batches ORDER BY id"))[
                0
            ][0] == "failed"
            assert (await database.fetch_all(db, "SELECT status FROM email_batches ORDER BY id"))[
                1
            ][0] == "sent"
        # A retry uses the saved body even if the archive and pending set change.
        await add_listing(path, 1, title="Changed after review", rent_pcm_pence=999_900)
        await saved_review(path, 4)
        phase = "accept"
        assert await deliver(notify_args(path, first, second)) == 0
        assert requests[2] == requests[0]
        assert [item["to"] for item in requests[2:]] == [[first], [first], [second]]
        for payload in requests[3:]:
            assert payload["subject"] == "1 new flats found"
            assert "https://www.openrent.co.uk/property-to-rent/london/4" in payload["html"]
            assert "https://www.openrent.co.uk/property-to-rent/london/1" not in payload["html"]
            assert "https://www.openrent.co.uk/property-to-rent/london/3" not in payload["html"]
        assert await deliver(notify_args(path, first, second)) == 0
        assert len(requests) == 5
        assert await deliver(notify_args(path, timeout_recipient)) == 1
        assert len(requests) == 6
        assert await deliver(notify_args(path, timeout_recipient)) == 1
        assert len(requests) == 6
        # Cancellation after the POST begins has the same uncertain acceptance rule.
        task = asyncio.create_task(deliver(notify_args(path, cancelled_recipient)))
        await asyncio.wait_for(cancellation_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(requests) == 7
        assert await deliver(notify_args(path, cancelled_recipient)) == 1
        assert len(requests) == 7

    await run()
    async with database.connect(path) as db:
        assert [
            tuple(row)
            for row in await database.fetch_all(
                db, "SELECT recipient, status, attempts FROM email_batches ORDER BY id"
            )
        ] == [
            (first, "sent", 2),
            (second, "sent", 1),
            (first, "sent", 1),
            (second, "sent", 1),
            (timeout_recipient, "unknown", 1),
            (cancelled_recipient, "unknown", 1),
        ]
        for recipient in (first, second):
            assert [
                row[0]
                for row in await database.fetch_all(
                    db,
                    "SELECT property_id FROM email_batch_items "
                    "WHERE recipient = ? ORDER BY property_id",
                    (recipient,),
                )
            ] == [1, 3, 4]
        assert not await database.fetch_all(db, "PRAGMA foreign_key_check")


@pytest.mark.asyncio
async def test_preview_needs_no_credentials_and_never_reserves_or_sends(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite"
    for pid, decision in ((1, "pass"), (2, "reject"), (3, "uncertain")):
        await saved_review(path, pid, decision)
    for name in ("RESEND_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not start a sender")

    monkeypatch.setattr(notifications, "ResendEmailSender", forbidden)
    preview = tmp_path / "preview" / "digest.html"
    args = notify_args(
        path, "self@example.com", email_preview=preview, env_file=tmp_path / "absent.env"
    )
    assert await deliver(args) == 0
    body = preview.read_text()
    assert "https://www.openrent.co.uk/property-to-rent/london/1" in body
    assert "https://www.openrent.co.uk/property-to-rent/london/2" not in body
    assert "https://www.openrent.co.uk/property-to-rent/london/3" in body
    assert await deliver(args) == 0
    assert preview.read_text() == body
    async with database.connect(path) as db:
        assert (await database.fetch_one(db, "SELECT count(*) FROM email_batches"))[0] == 0
        assert len(await database.pending_notifications(db, "self@example.com")) == 2


@pytest.mark.asyncio
async def test_nearby_amenity_tables_and_custom_additional_info_use_saved_amenities(tmp_path):
    path = tmp_path / "archive.sqlite"
    await saved_review(path, 1)
    await saved_review(path, 2)
    payload = gym_payload(519, 284, None, 780, 781, 47)
    name = '<script>alert("gym")</script> Leisure Centre'
    grocer_name = '<img src=x onerror="alert(1)"> Market'
    payload["gyms"][1]["name"] = name
    preview = tmp_path / "digest.html"
    config = notifications.configuration(
        notify_args(path, "self@example.com", email_preview=preview)
    )
    async with database.connect(path) as db:
        await database.store_amenity(db, 1, "gym", json.dumps(payload))
        await database.store_amenity(
            db,
            1,
            "grocer",
            json.dumps(
                {
                    "grocers": [
                        {"name": "Supermarket", "walking_seconds": 720},
                        {"name": grocer_name, "walking_seconds": 180},
                        {"name": "Unreachable market", "walking_seconds": None},
                        {"name": "Boundary market", "walking_seconds": 780},
                        {"name": "Distant market", "walking_seconds": 781},
                    ]
                }
            ),
        )
        # Groceries must still render when the property has no gym payload.
        await database.store_amenity(
            db,
            2,
            "grocer",
            json.dumps({"grocers": [{"name": "Only grocer", "walking_seconds": 150}]}),
        )
        await database.store_amenity(
            db,
            1,
            "park",
            json.dumps({"grocers": [{"name": "Wrong amenity type", "walking_seconds": 1}]}),
        )
        await database.store_amenity(
            db,
            1,
            "commute",
            json.dumps(
                {
                    "target": "Victoria",
                    "duration_minutes": 26,
                    "arrive_by": "2026-10-12T09:00:00+01:00",
                }
            ),
        )
        assert await notifications.send_pending(db, config, lambda message: None) == 0
        soup = BeautifulSoup(preview.read_text(), "html.parser")
        heading = soup.find("th", string="Gyms within 13 minutes")
        assert heading is not None
        table = heading.find_parent("table")
        assert [cell.get_text() for cell in table.find_all("td")] == [
            "Gym 6",
            "0.8 min on foot",
            name,
            "4.7 min on foot",
            "Gym 1",
            "8.7 min on foot",
        ]
        assert not soup.find("script")
        assert len(soup.find_all("th", string="Gyms within 13 minutes")) == 1
        grocer_headings = soup.find_all("th", string="Groceries within 13 minutes")
        assert len(grocer_headings) == 2
        expanders = soup.find_all("details")
        assert len(expanders) == 2
        assert all(not expander.has_attr("open") for expander in expanders)
        assert [expander.find("summary").get_text() for expander in expanders] == [
            "Groceries within 13 minutes (2)",
            "Groceries within 13 minutes (1)",
        ]
        assert all(heading.find_parent("details") for heading in grocer_headings)
        assert heading.find_parent("details") is None  # The gym table remains a regular table.
        assert [
            [cell.get_text() for cell in heading.find_parent("table").find_all("td")]
            for heading in grocer_headings
        ] == [
            [grocer_name, "3 min on foot", "Supermarket", "12 min on foot"],
            ["Only grocer", "2.5 min on foot"],
        ]
        assert not soup.find(attrs={"onerror": True})
        for excluded in (
            "Unreachable market",
            "Boundary market",
            "Distant market",
            "Wrong amenity type",
        ):
            assert excluded not in soup.get_text()
        assert "Public transport to Victoria" in soup.get_text()
        assert "26 min including walking (TfL); arrive by Mon 12 Oct 2026, 09:00" in soup.get_text()
        # Clients that strip native expander tags must retain the readable table contents.
        for element in soup.find_all(["details", "summary"]):
            element.unwrap()
        assert grocer_name in soup.get_text() and "Only grocer" in soup.get_text()
        assert len(soup.find_all("th", string="Groceries within 13 minutes")) == 2
        calls = []

        async def additional_info_generator(property_id):
            calls.append(property_id)
            amenities = await database.get_amenities(db, property_id)
            count = len(amenities.get("gym", {}).get("gyms", []))
            return f'<p class="custom-info">{count} saved gyms</p>'

        assert (
            await notifications.send_pending(
                db,
                config,
                lambda message: None,
                additional_info_generator=additional_info_generator,
            )
            == 0
        )
        soup = BeautifulSoup(preview.read_text(), "html.parser")
        assert sorted(calls) == [1, 2]
        assert sorted(item.get_text() for item in soup.select(".custom-info")) == [
            "0 saved gyms",
            "6 saved gyms",
        ]
        assert not soup.find("th", string="Groceries within 13 minutes")


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "-wal",
        "-shm",
        "-journal",
        ".scan.lock",
        ".dbos.sqlite",
        ".dbos.sqlite-wal",
        ".dbos.sqlite-shm",
    ],
)
def test_preview_cannot_overwrite_archive_or_system_files(tmp_path, suffix):
    path = tmp_path / "archive.sqlite"
    with pytest.raises(ValueError, match="SQLite database"):
        notifications.configuration(
            notify_args(path, "self@example.com", email_preview=Path(str(path) + suffix))
        )


def test_preview_protects_symlinks_hardlinks_criteria_and_environment(tmp_path):
    path = tmp_path / "archive.sqlite"
    path.write_bytes(b"Archive must survive")
    symlink = tmp_path / "alias.sqlite"
    symlink.symlink_to(path)
    hardlink = tmp_path / "hardlink.sqlite"
    hardlink.hardlink_to(path)
    for alias in (symlink, hardlink):
        with pytest.raises(ValueError, match="SQLite database"):
            notifications.configuration(notify_args(path, "self@example.com", email_preview=alias))
    criteria = tmp_path / "criteria.txt"
    with pytest.raises(ValueError, match="criteria file"):
        notifications.configuration(
            notify_args(path, "self@example.com", email_preview=criteria, criteria_file=criteria)
        )
    env_file = tmp_path / "private.env"
    with pytest.raises(ValueError, match="environment file"):
        notifications.configuration(
            notify_args(path, "self@example.com", email_preview=env_file, env_file=env_file)
        )
    assert path.read_bytes() == b"Archive must survive"


@pytest.mark.parametrize(
    "recipients, message",
    [
        ((), "at least one --email-to"),
        (("first@example.com", "second@example.com"), "exactly one"),
        (("Name <self@example.com>",), "single plain email address"),
    ],
)
def test_invalid_preview_recipients_are_rejected(tmp_path, recipients, message):
    with pytest.raises(ValueError, match=message):
        notifications.configuration(
            notify_args(
                tmp_path / "archive.sqlite", *recipients, email_preview=tmp_path / "preview.html"
            )
        )


@pytest.mark.asyncio
async def test_token_files_environment_override_and_retry_saved_digest(
    tmp_path, monkeypatch, mock_email_transport
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RESEND_TOKEN", raising=False)
    path = tmp_path / "archive.sqlite"
    for pid, decision in ((1, "pass"), (2, "reject"), (3, "uncertain")):
        await saved_review(path, pid, decision)
    token_file = tmp_path / "custom.env"
    token_file.write_text('RESEND_TOKEN="test-file-token"\n')
    recipient = "self@example.com"
    requests, headers, keys = [], [], []

    async def post(request):
        requests.append(json.loads(request.content))
        headers.append(request.headers["Authorization"])
        keys.append(request.headers["Idempotency-Key"])
        if len(requests) == 1:
            return httpx.Response(400, json={"message": "Definite rejection"})
        return accepted(recipient)

    mock_email_transport(post)
    args = notify_args(path, recipient, env_file=token_file)
    assert await deliver(args) == 1
    assert headers == ["Bearer test-file-token"]
    await add_listing(path, 1, title="Changed after judging", rent_pcm_pence=999900)
    await saved_review(path, 4)
    monkeypatch.setenv("RESEND_TOKEN", "test-environment-token")
    assert await deliver(args) == 0
    assert requests[1] == requests[0] and keys[1] == keys[0]
    assert keys[2] != keys[0] and requests[2]["subject"] == "1 new flats found"
    assert headers[1:] == ["Bearer test-environment-token"] * 2
    assert await deliver(args) == 0 and len(requests) == 3
    await saved_review(path, 5)
    monkeypatch.delenv("RESEND_TOKEN")
    (tmp_path / ".env").write_text("RESEND_TOKEN=test-default-token\n")
    assert await deliver(notify_args(path, recipient)) == 0
    assert headers[-1] == "Bearer test-default-token"
    async with database.connect(path) as db:
        assert [
            tuple(row)
            for row in await database.fetch_all(
                db, "SELECT status,attempts FROM email_batches ORDER BY id"
            )
        ] == [("sent", 2), ("sent", 1), ("sent", 1)]
