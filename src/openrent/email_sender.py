"""Async email providers with conservative delivery accounting."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from uuid import UUID

import httpx

SENDER_EMAIL = "notifications@flats.spanashis.com"
_RECIPIENT = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z"
)


class EmailSendError(RuntimeError):
    """The provider explicitly rejected the request; no acceptance was recorded."""


class EmailSendIndeterminate(EmailSendError):
    """The request may have been accepted; retrying could send duplicate email."""


@dataclass(frozen=True, slots=True)
class ResendEmailConfig:
    api_token: str = field(repr=False)
    timeout: float = 30

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_token, str)
            or not self.api_token.strip()
            or any(ord(character) <= 32 or ord(character) == 127 for character in self.api_token)
        ):
            raise ValueError("Resend API token must be nonempty and contain no whitespace.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Resend email timeout must be a positive finite number.")


@dataclass(frozen=True, slots=True)
class SendReceipt:
    provider_message_id: str | None
    accepted: bool


def validate_recipient(recipient: str) -> str:
    """Validate a single plain address, without display names or address lists."""
    if (
        not isinstance(recipient, str)
        or len(recipient) > 254
        or not _RECIPIENT.fullmatch(recipient)
    ):
        raise ValueError("Email recipient must be a single plain email address.")
    local, domain = recipient.rsplit("@", 1)
    if (
        len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or "." not in domain
        or any(
            not label or label.startswith("-") or label.endswith("-") for label in domain.split(".")
        )
    ):
        raise ValueError("Email recipient must be a single plain email address.")
    return recipient


def _validate_message(recipient: str, subject: str, html: str, text: str) -> None:
    validate_recipient(recipient)
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in subject)
    ):
        raise ValueError("Email subject must be nonempty and contain no control characters.")
    if not isinstance(html, str) or not isinstance(text, str) or not (html or text):
        raise ValueError("Email needs HTML or plain text content.")


class ResendEmailSender:
    """Submit one digest to Resend; acceptance does not guarantee inbox delivery.

    A caller may supply a durable batch key for Resend's 24-hour idempotency
    window. This client does not retry requests automatically. Unknown outcomes
    must remain reserved in local delivery tracking, including after that window.
    """

    def __init__(
        self, config: ResendEmailConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.config = config
        self._client = client

    async def send(
        self,
        recipient: str,
        subject: str,
        html: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> SendReceipt:
        _validate_message(recipient, subject, html, text)
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 256
            or any(not 33 <= ord(character) <= 126 for character in idempotency_key)
        ):
            raise ValueError("Email idempotency key must have 1–256 visible ASCII characters.")
        payload = {
            "from": SENDER_EMAIL,
            "to": [recipient],
            "subject": subject,
            "html": html,
            "text": text,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_token}",
            "User-Agent": "openrent-fetch/0.1",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if self._client is not None:
            return await self._send(self._client, payload, headers)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await self._send(client, payload, headers)

    async def _send(
        self, client: httpx.AsyncClient, payload: dict[str, object], headers: dict[str, str]
    ) -> SendReceipt:
        try:
            response = await client.post(
                "https://api.resend.com/emails",
                headers=headers,
                json=payload,
                timeout=self.config.timeout,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise EmailSendIndeterminate(
                "Resend email acceptance is unknown after a network failure; "
                "the request was not retried."
            ) from None
        # Conflicts can mean another request with this key is already being processed.
        if response.status_code in (408, 409):
            raise EmailSendIndeterminate(
                f"Resend returned HTTP {response.status_code}; email acceptance is unknown."
            )
        if 400 <= response.status_code < 500:
            raise EmailSendError(
                f"Resend rejected the email request (HTTP {response.status_code})."
            )
        if not 200 <= response.status_code < 300:
            raise EmailSendIndeterminate(
                f"Resend returned HTTP {response.status_code}; email acceptance is unknown."
            )
        try:
            body = response.json()
            message_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(message_id, str):
                raise TypeError
            UUID(message_id)
        except (ValueError, TypeError, AttributeError):
            raise EmailSendIndeterminate(
                "Resend returned an invalid email acceptance receipt."
            ) from None
        return SendReceipt(provider_message_id=message_id, accepted=True)
