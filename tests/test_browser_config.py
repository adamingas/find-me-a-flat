from types import SimpleNamespace

import pytest

from openrent.browser_config import configuration
from openrent.browser_notifications import ChallengeNotifier
from openrent.browser_transport import BrowserTransport
from openrent.email_sender import ResendEmailSender


def test_browser_configuration_is_opt_in_and_requires_own_session(tmp_path):
    assert configuration(SimpleNamespace()) is None
    with pytest.raises(ValueError, match="cannot be combined"):
        configuration(
            SimpleNamespace(browser_endpoint="http://localhost:8091", cookie_file="cookies.txt")
        )
    with pytest.raises(ValueError, match="requires"):
        configuration(SimpleNamespace(browser_endpoint="http://localhost:8091", cookie_file=None))


@pytest.mark.asyncio
async def test_configuration_wires_durable_email_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_TOKEN", "test-only-token")
    token_file = tmp_path / "token"
    token_file.write_text("a" * 64)
    transport = configuration(
        SimpleNamespace(
            browser_endpoint="http://localhost:8091",
            cookie_file=None,
            browser_token_file=token_file,
            browser_viewer_url="http://tailnet:5801/vnc.html",
            browser_alert_to=("me@example.com", "me@example.com"),
            env_file=str(tmp_path / "unused.env"),
            email_timeout=30,
            db=tmp_path / "flats.sqlite",
        )
    )
    try:
        assert isinstance(transport, BrowserTransport)
        assert isinstance(transport.notify, ChallengeNotifier)
        assert transport.notify.recipients == ("me@example.com",)
        assert transport.notify.path == tmp_path / "flats.sqlite.browser-alert.json"
        assert isinstance(transport.notify.sender, ResendEmailSender)
    finally:
        await transport.aclose()
