"""Send passed and uncertain reviews once per recipient through an email provider."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .email_digest import DigestListing, render_digest
from .email_sender import (
    CloudflareEmailConfig,
    CloudflareEmailSender,
    EmailSendError,
    EmailSendIndeterminate,
    ResendEmailConfig,
    ResendEmailSender,
    validate_recipient,
)
from .export import validate_destination
from .locking import ScanLock
from .review_db import AsyncReviewDatabase


@dataclass(frozen=True)
class NotificationConfig:
    recipients: tuple[str, ...]
    provider: ResendEmailConfig | CloudflareEmailConfig | None = None
    preview: Path | None = None


def configuration(args) -> NotificationConfig | None:
    recipients = tuple(
        dict.fromkeys(
            validate_recipient(address.strip().casefold())
            for address in getattr(args, "email_to", ())
        )
    )
    preview = getattr(args, "email_preview", None)
    if not recipients and preview is None:
        return None
    if not recipients:
        raise ValueError("Supply at least one --email-to ADDRESS.")
    env_file = Path(getattr(args, "env_file", None) or ".env")
    if preview is not None:
        if len(recipients) != 1:
            raise ValueError("An --email-preview file requires exactly one --email-to recipient.")
        preview = Path(preview)
        validate_destination(args.db, preview)
        criteria_file = getattr(args, "criteria_file", None)
        if criteria_file is not None and preview.resolve() == Path(criteria_file).resolve():
            raise ValueError("Email preview must not overwrite the criteria file.")
        if preview.resolve() == env_file.resolve():
            raise ValueError("Email preview must not overwrite the environment file.")
        return NotificationConfig(recipients, preview=preview)
    if getattr(args, "dry_run", False):
        return NotificationConfig(recipients)
    if env_file.exists() and not env_file.is_file():
        raise ValueError("The environment file must be a regular file.")
    settings = dict(dotenv_values(env_file)) if env_file.is_file() else {}
    settings.update(os.environ)
    timeout = getattr(args, "email_timeout", 30)
    provider_name = getattr(args, "email_provider", "resend")
    if provider_name == "resend":
        token = settings.get("RESEND_TOKEN") or ""
        if not token:
            raise ValueError("Email sending needs RESEND_TOKEN in the environment or env file.")
        return NotificationConfig(recipients, provider=ResendEmailConfig(token, timeout=timeout))
    if provider_name != "cloudflare":
        raise ValueError("Email provider must be resend or cloudflare.")
    account_id = getattr(args, "cloudflare_account_id", None) or settings.get(
        "CLOUDFLARE_ACCOUNT_ID", ""
    )
    token = settings.get("CLOUDFLARE_API_TOKEN") or ""
    if not account_id or not token:
        raise ValueError(
            "Email sending needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
            "(or --cloudflare-account-id for the account ID)."
        )
    provider = CloudflareEmailConfig(account_id, token, timeout=timeout)
    return NotificationConfig(recipients, provider=provider)


async def send_pending(
    db: AsyncReviewDatabase,
    config: NotificationConfig,
    report=print,
    *,
    property_ids: set[int] | None = None,
) -> int:
    """The caller holds the review lock throughout selection, sending and recording."""

    async def eligible(recipient):
        records = await db.pending_notifications(recipient)
        if property_ids is not None:
            records = [record for record in records if record["property_id"] in property_ids]
        return records

    if config.preview is not None:
        records = await eligible(config.recipients[0])
        if not records:
            report("No new passed or uncertain flats to preview.")
            return 0
        digest = render_digest([_listing(record) for record in records])
        await asyncio.to_thread(_write_preview, config.preview, digest.html)
        report(f"Email preview: {config.preview.resolve()} ({digest.subject}).")
        return 0
    if config.provider is None:
        raise ValueError("Email provider configuration is required for sending email.")
    use_resend = isinstance(config.provider, ResendEmailConfig)
    provider_name = "Resend" if use_resend else "Cloudflare"
    sender = (
        ResendEmailSender(config.provider) if use_resend else CloudflareEmailSender(config.provider)
    )
    failures = 0
    for recipient in config.recipients:
        # Retry an explicitly failed request with its saved content. A previous
        # indeterminate attempt stays reserved until its delivery is reconciled.
        while True:
            batch = await db.pending_email_batch(recipient)
            if batch is not None and batch["status"] == "unknown":
                report(
                    f"Email acceptance is unknown for {recipient}, batch {batch['id']}; "
                    "check the email provider's logs before retrying."
                )
                failures += 1
                break
            if batch is None:
                records = await eligible(recipient)
                if not records:
                    report(f"No new passed or uncertain flats to email to {recipient}.")
                    break
                digest = render_digest([_listing(record) for record in records])
                batch = await db.create_email_batch(
                    recipient,
                    [record["review_id"] for record in records],
                    digest.subject,
                    digest.html,
                    digest.text,
                )
            if not await db.mark_email_sending(batch["id"]):
                raise ValueError("Email batch could not be claimed for sending.")
            try:
                send_options = {"idempotency_key": _batch_key(batch)} if use_resend else {}
                receipt = await sender.send(
                    recipient,
                    batch["subject"],
                    batch["html_body"],
                    batch["text_body"],
                    **send_options,
                )
            except asyncio.CancelledError:
                await db.fail_email_batch(
                    batch["id"], "Sending interrupted; acceptance is unknown.", indeterminate=True
                )
                raise
            except EmailSendIndeterminate as exc:
                await db.fail_email_batch(batch["id"], str(exc), indeterminate=True)
                report(f"Email acceptance unknown for {recipient}: {exc}")
                failures += 1
                break
            except (EmailSendError, ValueError) as exc:
                await db.fail_email_batch(batch["id"], str(exc))
                report(f"Email to {recipient} failed: {exc}")
                failures += 1
                break
            if not receipt.accepted:
                await db.fail_email_batch(
                    batch["id"],
                    f"{provider_name} reported the recipient bounced or was suppressed.",
                )
                report(f"{provider_name} did not accept the email for {recipient}.")
                failures += 1
                break
            await db.finish_email_batch(batch["id"], receipt.provider_message_id)
            report(f"{provider_name} accepted email to {recipient}: {batch['subject']}.")
            # A saved retry can predate additional eligible reviews. Send those
            # in a fresh batch after this one has been recorded successfully.
    return int(bool(failures))


def _batch_key(batch: dict) -> str:
    identity = f"{batch['recipient']}\0{batch['created_at']}\0{batch['id']}\0{batch['sender']}"
    return "openrent-" + hashlib.sha256(identity.encode()).hexdigest()


def _listing(record: dict) -> DigestListing:
    return DigestListing(
        property_id=record["property_id"],
        title=record["title"],
        url=record["url"],
        property_data=record["property"],
        review_result=record["result"],
    )


def _write_preview(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


async def notify_command(args) -> int:
    if not args.db.is_file():
        raise ValueError(f"Database does not exist: {args.db}")
    config = configuration(args)
    if config is None:
        raise ValueError("Supply --email-to ADDRESS to send or preview a digest.")
    report = print if not args.quiet else lambda _: None
    with ScanLock(Path(str(args.db.resolve()) + ".review")):
        async with AsyncReviewDatabase(args.db) as db:
            return await send_pending(db, config, report)
