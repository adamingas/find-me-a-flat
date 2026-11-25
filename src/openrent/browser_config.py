"""Compose CLI settings, browser transport, and attention emails."""

from pathlib import Path
from urllib.parse import urlsplit

from .browser_notifications import ChallengeNotifier
from .browser_transport import BrowserTransport
from .email_sender import ResendEmailConfig, ResendEmailSender, validate_recipient
from .settings import load_settings


def configuration(args):
    endpoint = getattr(args, "browser_endpoint", None)
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.query:
        raise ValueError(
            "Browser endpoint must be an HTTP(S) service URL without credentials or query."
        )
    if args.cookie_file:
        raise ValueError(
            "--cookie-file cannot be combined with --browser-endpoint; use the browser profile."
        )
    token_file = getattr(args, "browser_token_file", None)
    viewer_url = getattr(args, "browser_viewer_url", None)
    if not token_file or not viewer_url:
        raise ValueError("Browser mode requires --browser-token-file and --browser-viewer-url.")
    viewer = urlsplit(viewer_url)
    if viewer.scheme not in {"http", "https"} or not viewer.hostname or viewer.username:
        raise ValueError("Browser viewer must be an HTTP(S) URL without credentials.")
    token = Path(token_file).read_text().strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("Browser API token must contain at least 32 non-whitespace characters.")
    recipients = tuple(dict.fromkeys(validate_recipient(r) for r in args.browser_alert_to))
    if not recipients:
        raise ValueError("Browser mode requires at least one --browser-alert-to recipient.")
    token_config = load_settings(Path(args.env_file or ".env").resolve()).resend_token
    sender = ResendEmailSender(ResendEmailConfig(token_config, args.email_timeout))
    notifier = ChallengeNotifier(
        Path(str(args.db) + ".browser-alert.json"), viewer_url, recipients, sender
    )
    return BrowserTransport(endpoint, token, notifier)
