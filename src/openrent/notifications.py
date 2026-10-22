"""Send passed and uncertain reviews once per recipient through an email provider."""

from __future__ import annotations

import asyncio
import hashlib
import html
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from dbos import DBOS

from . import db as database
from .email_digest import DigestListing, render_digest
from .email_sender import (
    EmailSendError,
    EmailSendIndeterminate,
    ResendEmailConfig,
    ResendEmailSender,
    validate_recipient,
)
from .settings import load_settings


@dataclass(frozen=True)
class NotificationConfig:
    recipients: tuple[str, ...]
    provider: ResendEmailConfig | None = None
    preview: Path | None = None


def _validate_preview_destination(db_path: Path, output: Path) -> None:
    """Reject email previews that would overwrite an archive or its working files."""
    db_path = Path(db_path).resolve()
    output = Path(output)
    resolved_output = output.resolve()
    system_path = Path(str(db_path) + ".dbos.sqlite")
    protected = (
        *(
            Path(str(database) + suffix)
            for database in (db_path, system_path)
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
        Path(str(db_path) + ".scan.lock"),
    )
    for path in protected:
        if resolved_output == path.resolve() or (
            output.exists() and path.exists() and os.path.samefile(output, path)
        ):
            raise ValueError(
                "Email preview must not overwrite the SQLite database, its sidecars, or scan lock."
            )


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
        _validate_preview_destination(
            args.database if isinstance(args, NotificationSettings) else args.db, preview
        )
        criteria_file = getattr(args, "criteria_file", None)
        if criteria_file is not None and preview.resolve() == Path(criteria_file).resolve():
            raise ValueError("Email preview must not overwrite the criteria file.")
        if preview.resolve() == env_file.resolve():
            raise ValueError("Email preview must not overwrite the environment file.")
        return NotificationConfig(recipients, preview=preview)
    timeout = getattr(args, "email_timeout", 30)
    token = load_settings(env_file.resolve()).resend_token
    if not token:
        raise ValueError("Email sending needs RESEND_TOKEN in the environment or env file.")
    return NotificationConfig(recipients, provider=ResendEmailConfig(token, timeout=timeout))


async def send_pending(
    db: aiosqlite.Connection,
    config: NotificationConfig,
    report=print,
    *,
    additional_info_generator: Callable[[int], Awaitable[str]] | None = None,
) -> int:
    """The notification queue serializes delivery; review continues independently."""

    if additional_info_generator is None:
        additional_info_generator = AmenityAdditionalInfoGenerator(db)

    async def listings(records):
        listings = []
        for record in records:
            record = {
                **record,
                "property": {
                    **record["property"],
                    "amenities": await database.get_amenities(db, record["property_id"]),
                },
            }
            listings.append(
                _listing(record, await additional_info_generator(record["property_id"]))
            )
        return listings

    if config.preview is not None:
        records = await database.pending_notifications(db, config.recipients[0])
        if not records:
            report("No new passed or uncertain flats to preview.")
            return 0
        digest = render_digest(await listings(records))
        await asyncio.to_thread(_write_preview, config.preview, digest.html)
        report(f"Email preview: {config.preview.resolve()} ({digest.subject}).")
        return 0
    if config.provider is None:
        raise ValueError("Email provider configuration is required for sending email.")
    sender = ResendEmailSender(config.provider)
    failures = 0
    for recipient in config.recipients:
        # Retry an explicitly failed request with its saved content. A previous
        # indeterminate attempt stays reserved until its delivery is reconciled.
        while True:
            batch = await database.pending_email_batch(db, recipient)
            if batch is not None and batch["status"] == "unknown":
                report(
                    f"Email acceptance is unknown for {recipient}, batch {batch['id']}; "
                    "check the email provider's logs before retrying."
                )
                failures += 1
                break
            if batch is None:
                records = await database.pending_notifications(db, recipient)
                if not records:
                    report(f"No new passed or uncertain flats to email to {recipient}.")
                    break
                digest = render_digest(await listings(records))
                batch = await database.create_email_batch(
                    db,
                    recipient,
                    [record["review_id"] for record in records],
                    digest.subject,
                    digest.html,
                    digest.text,
                )
            if not await database.mark_email_sending(db, batch["id"]):
                raise ValueError("Email batch could not be claimed for sending.")
            try:
                receipt = await sender.send(
                    recipient,
                    batch["subject"],
                    batch["html_body"],
                    batch["text_body"],
                    idempotency_key=_batch_key(batch),
                )
            except asyncio.CancelledError:
                await database.fail_email_batch(
                    db,
                    batch["id"],
                    "Sending interrupted; acceptance is unknown.",
                    indeterminate=True,
                )
                raise
            except EmailSendIndeterminate as exc:
                await database.fail_email_batch(db, batch["id"], str(exc), indeterminate=True)
                report(f"Email acceptance unknown for {recipient}: {exc}")
                failures += 1
                break
            except (EmailSendError, ValueError) as exc:
                await database.fail_email_batch(db, batch["id"], str(exc))
                report(f"Email to {recipient} failed: {exc}")
                failures += 1
                break
            if not receipt.accepted:
                await database.fail_email_batch(
                    db,
                    batch["id"],
                    "Resend reported the recipient bounced or was suppressed.",
                )
                report(f"Resend did not accept the email for {recipient}.")
                failures += 1
                break
            await database.finish_email_batch(db, batch["id"], receipt.provider_message_id)
            report(f"Resend accepted email to {recipient}: {batch['subject']}.")
            # A saved retry can predate additional eligible reviews. Send those
            # in a fresh batch after this one has been recorded successfully.
    return int(bool(failures))


def _batch_key(batch: dict) -> str:
    identity = f"{batch['recipient']}\0{batch['created_at']}\0{batch['id']}\0{batch['sender']}"
    return "openrent-" + hashlib.sha256(identity.encode()).hexdigest()


class AmenityAdditionalInfoGenerator:
    """List saved gyms and groceries less than 13 minutes away on foot, nearest first."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def __call__(self, property_id: int) -> str:
        style = "padding:7px 9px;border:1px solid #ddd;text-align:left"
        tables = []
        for amenity_type, collection, heading, unnamed in (
            ("gym", "gyms", "Gyms", "Unnamed gym"),
            ("grocer", "grocers", "Groceries", "Unnamed grocer"),
        ):
            rows = await database.query(
                self.db,
                "SELECT json_extract(p.value, '$.name') AS name, "
                "json_extract(p.value, '$.walking_seconds') AS seconds "
                "FROM amenities a CROSS JOIN json_each(a.data, ?) p "
                "WHERE a.property_id = ? AND a.type = ? "
                "AND json_extract(p.value, '$.walking_seconds') < 13 * 60 "
                "ORDER BY seconds, name",
                (f"$.{collection}", property_id, amenity_type),
            )
            if not rows:
                continue
            body = []
            for place in rows:
                name = html.escape(place["name"] or unnamed)
                minutes = f"{place['seconds'] / 60:.1f}".rstrip("0").rstrip(".")
                body.append(
                    f'<tr><td style="{style}">{name}</td>'
                    f'<td style="{style}">{minutes} min on foot</td></tr>'
                )
            body_html = "".join(body)
            table = (
                '<table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:12px">'
                f'<tr><th style="{style}">{heading} within 13 minutes</th>'
                f'<th style="{style}">Walk</th></tr>'
                f"{body_html}</table>"
            )
            if amenity_type == "grocer":
                # Keep the table visible when an email client strips these unsupported tags.
                table = (
                    '<details style="margin-top:12px;font-size:13px">'
                    f'<summary style="{style};cursor:pointer;color:#1d4ed8">'
                    f"Groceries within 13 minutes ({len(rows)})</summary>"
                    f"{table}</details>"
                )
            tables.append(table)
        return "".join(tables)


def _listing(record: dict, additional_info_html: str = "") -> DigestListing:
    return DigestListing(
        property_id=record["property_id"],
        title=record["title"],
        url=record["url"],
        property_data=record["property"],
        review_result=record["result"],
        additional_info_html=additional_info_html,
    )


def _write_preview(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


@dataclass(frozen=True)
class NotificationSettings:
    database: Path
    email_to: tuple[str, ...]
    email_preview: Path | None
    email_timeout: float
    env_file: Path


@DBOS.step()
async def deliver_digest(settings: NotificationSettings) -> int:
    from .runtime import resources

    runtime = resources()
    config = configuration(settings)
    if config is None:
        return 0
    return await send_pending(runtime.db, config, runtime.report)


@DBOS.workflow()
async def notify(scheduled_time, settings: NotificationSettings) -> None:
    await deliver_digest(settings)
