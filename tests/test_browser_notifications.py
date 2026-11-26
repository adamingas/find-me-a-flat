import base64
import json
import logging

import httpx
import pytest

from openrent.browser_notifications import ChallengeNotifier
from openrent.browser_transport import BrowserTransport
from openrent.email_sender import EmailSendIndeterminate


@pytest.mark.asyncio
async def test_challenge_emails_viewer_link_once_then_resumes_original_request(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="openrent.browser_notifications")
    messages, requests = [], []
    viewer_url = "http://tailnet:5801/vnc.html?autoconnect=1&resize=remote"

    class Sender:
        async def send(self, recipient, subject, html, text, **kwargs):
            messages.append((recipient, subject, html, text, kwargs))

    def worker(request):
        requests.append(json.loads(request.content))
        if len(requests) <= 2:
            return httpx.Response(423, json={"episode": "captcha-one"})
        return httpx.Response(
            200,
            json={
                "url": requests[-1]["url"],
                "status": 200,
                "headers": {},
                "body": base64.b64encode(b"listing results").decode(),
            },
        )

    notifier = ChallengeNotifier(
        tmp_path / "alerts.json", viewer_url, ("me@example.com",), Sender()
    )
    transport = BrowserTransport(
        "http://worker",
        "private-api-token",
        notifier,
        poll_seconds=0,
        http_transport=httpx.MockTransport(worker),
    )
    try:
        result = await transport.get("https://www.openrent.co.uk/search", params={"term": "London"})
        assert result.content == b"listing results"
        assert len(requests) == 3
        assert requests[0] == requests[1] == requests[2]
        assert len(messages) == 1
        recipient, subject, html, text, options = messages[0]
        assert recipient == "me@example.com"
        assert subject == "OpenRent needs browser verification"
        assert viewer_url in text
        assert 'href="http://tailnet:5801/vnc.html?autoconnect=1&amp;resize=remote"' in html
        assert "private-api-token" not in html + text
        assert options["idempotency_key"].startswith("browser-")
        assert caplog.text.count("Browser alert accepted by email provider") == 1
        assert "episode=captcha-one recipient=me@example.com" in caplog.text
        assert "private-api-token" not in caplog.text
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_reservation_failure_does_not_silence_the_episode(tmp_path):
    calls = []

    class Sender:
        async def send(self, *args, **kwargs):
            calls.append(args)

    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory")
    notifier = ChallengeNotifier(
        blocked_parent / "alerts.json",
        "http://tailnet:5801/vnc.html",
        ("me@example.com",),
        Sender(),
    )
    with pytest.raises(OSError):
        await notifier("one")
    blocked_parent.unlink()
    await notifier("one")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_alert_reservation_survives_restart_and_unknown_delivery(tmp_path):
    calls = []

    class Sender:
        async def send(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise EmailSendIndeterminate("network timeout")

    path = tmp_path / "alert.json"
    for episode in ["first", "first", "second"]:
        notifier = ChallengeNotifier(
            path, "http://tailnet:5801/vnc.html", ("me@example.com",), Sender()
        )
        await notifier(episode)
    assert len(calls) == 1
    assert "Open browser</a>" in calls[0][0][2]
    assert json.loads(path.read_text())["episode"] == "first"
    assert path.stat().st_mode & 0o777 == 0o600
