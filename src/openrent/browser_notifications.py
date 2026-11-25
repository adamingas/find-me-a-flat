"""Browser-attention email delivery and durable duplicate suppression."""

import hashlib
import html
import json
import logging
import os
import time
from pathlib import Path

from .email_sender import EmailSendError

logger = logging.getLogger(__name__)


class ChallengeNotifier:
    """Reserve before sending: ambiguous delivery must not create an alert storm."""

    def __init__(self, path, viewer_url, recipients, sender, cooldown=3600):
        self.path = Path(path)
        self.viewer_url = viewer_url
        self.recipients = recipients
        self.sender = sender
        self.cooldown = cooldown
        self._seen = None

    async def __call__(self, episode):
        if episode == self._seen:
            return
        logger.warning("OpenRent needs browser attention. Open %s", self.viewer_url)
        try:
            previous = json.loads(self.path.read_text())
        except FileNotFoundError:
            previous = {}
        now = time.time()
        if (
            previous.get("episode") == episode
            or now - previous.get("attempted_at", 0) < self.cooldown
        ):
            self._seen = episode
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({"episode": episode, "attempted_at": now}, file)
        temporary.replace(self.path)
        self._seen = episode
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
