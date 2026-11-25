"""Authenticated browser RPC and a cancellable human handoff."""

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .browser_protocol import MAX_BODY_BYTES, allowed_url
from .email_sender import EmailSendError, ResendEmailConfig, ResendEmailSender, validate_recipient
from .fetch_transport import (
    BeforeRequest,
    FetchTransport,
    QueryParams,
    RequestHeaders,
    ResponseTooLarge,
)
from .settings import load_settings

logger = logging.getLogger(__name__)


class ChallengeNotifier:
    """Reserve before sending: ambiguous delivery must not create an alert storm."""

    def __init__(self, path, viewer_url, recipients=(), sender=None, cooldown=3600):
        self.path = Path(path)
        self.viewer_url = viewer_url
        self.recipients = recipients
        self.sender = sender
        self.cooldown = cooldown
        self._seen = None

    async def __call__(self, episode):
        if episode == self._seen:
            return
        self._seen = episode
        logger.warning("OpenRent needs browser attention. Open %s", self.viewer_url)
        if not self.sender:
            return
        try:
            previous = json.loads(self.path.read_text())
        except FileNotFoundError:
            previous = {}
        now = time.time()
        if (
            previous.get("episode") == episode
            or now - previous.get("attempted_at", 0) < self.cooldown
        ):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({"episode": episode, "attempted_at": now}, file)
        temporary.replace(self.path)
        text = (
            "OpenRent needs your attention in the server's browser. "
            "Connect to your tailnet, open the link, and complete any verification shown. "
            "Leave the page open when finished; collection resumes automatically.\n\n"
            + self.viewer_url
        )
        for recipient in self.recipients:
            key = hashlib.sha256(f"{episode}:{recipient}".encode()).hexdigest()
            try:
                await self.sender.send(
                    recipient,
                    "OpenRent needs browser verification",
                    f'<p>{html.escape(text)}</p><p><a href="{html.escape(self.viewer_url, quote=True)}">Open browser</a></p>',
                    text,
                    idempotency_key=f"browser-{key}",
                )
            except EmailSendError as exc:
                # The reservation remains for explicit rejection AND uncertain acceptance.
                logger.error(
                    "Browser alert delivery failed (%s); see viewer link above.", type(exc).__name__
                )


class BrowserTransport(FetchTransport):
    def __init__(
        self,
        endpoint,
        token,
        notify,
        *,
        poll_seconds=10,
        timeout=90,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.notify = notify
        self.poll_seconds = poll_seconds
        self._lock = asyncio.Lock()
        self.http = httpx.AsyncClient(
            transport=http_transport,
            base_url=endpoint.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

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
        async with self._lock:
            while True:
                if before_request:
                    # Pace inside the shared browser lock, including challenge replays.
                    await before_request(request)
                response = await self.http.post(
                    "fetch", json={"url": str(request.url), "headers": dict(headers or {})}
                )
                if response.status_code == 423:
                    episode = response.json()["episode"]
                    try:
                        await self.notify(episode)
                    except (OSError, ValueError, EmailSendError) as exc:
                        logger.error("Browser alert unavailable (%s).", type(exc).__name__)
                    # Cancellation releases this lock immediately on daemon shutdown.
                    await asyncio.sleep(self.poll_seconds)
                    continue
                response.raise_for_status()
                result = response.json()
                if not allowed_url(result["url"]):
                    raise ValueError("Browser returned a non-OpenRent URL.")
                encoded = result["body"]
                if len(encoded) > 4 * ((MAX_BODY_BYTES + 2) // 3):
                    raise ValueError("Browser response exceeds the 25 MiB limit.")
                content = base64.b64decode(encoded, validate=True)
                if len(content) > MAX_BODY_BYTES:
                    raise ValueError("Browser response exceeds the 25 MiB limit.")
                if max_bytes is not None and len(content) > max_bytes:
                    raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
                return httpx.Response(
                    result["status"],
                    headers=result["headers"],
                    content=content,
                    request=httpx.Request("GET", result["url"]),
                )


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
    sender = None
    if recipients:
        token_config = load_settings(Path(args.env_file or ".env").resolve()).resend_token
        sender = ResendEmailSender(ResendEmailConfig(token_config, args.email_timeout))
    notifier = ChallengeNotifier(
        Path(str(args.db) + ".browser-alert.json"), viewer_url, recipients, sender
    )
    return BrowserTransport(endpoint, token, notifier)
