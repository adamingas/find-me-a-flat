"""Exercise review-to-email accounting with real SQLite and mocked provider HTTP."""

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from test_review import add_listing, config, judgement

from openrent import cli, notifications, review
from openrent.email_sender import (
    SENDER_EMAIL,
    CloudflareEmailConfig,
    CloudflareEmailSender,
    ResendEmailSender,
)
from openrent.review_db import ReviewDatabase


def saved_review(path, property_id, decision="pass"):
    add_listing(path, property_id)
    cfg = config(path)
    with ReviewDatabase(path) as db:
        db.register_profile(cfg.profile_key, cfg.criteria, cfg.model, cfg.backend)
        claimed = db.claim_review(cfg.profile_key, property_id)
        assert claimed is not None
        assert db.complete_review(claimed[0], judgement(decision))


def notify_args(path, *recipients):
    return SimpleNamespace(
        db=path,
        email_to=recipients,
        email_preview=None,
        email_provider="cloudflare",
        env_file=None,
        cloudflare_account_id="test-account",
        email_timeout=1,
        quiet=True,
    )


def accepted(recipient):
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": {"queued": [recipient], "message_id": "test-provider-message"},
        },
    )


def test_bounded_review_finishes_entire_cohort_before_emailing_pass_and_uncertain(
    tmp_path, monkeypatch
):
    path = tmp_path / "archive.sqlite"
    for property_id in range(1, 5):
        add_listing(path, property_id)
    saved_review(path, 99)  # An earlier unemailed pass lies outside the chosen cohort.
    recipient = "self@example.com"
    provider = CloudflareEmailConfig("test-account", "test-placeholder", timeout=1)
    cfg = replace(
        config(path),
        concurrency=2,
        limit=3,
        notifications=notifications.NotificationConfig((recipient,), provider),
    )
    judged = []
    requests = []
    running = maximum = 0

    async def judge(snapshot, images, criteria, **kwargs):
        nonlocal running, maximum
        judged.append(snapshot["id"])
        assert len(images) == 2 and criteria == cfg.criteria
        assert kwargs["model"] == cfg.model
        running += 1
        maximum = max(maximum, running)
        try:
            await asyncio.sleep(0.01)
            return judgement({1: "reject", 2: "pass", 3: "uncertain"}[snapshot["id"]])
        finally:
            running -= 1

    async def post(request):
        assert request.method == "POST"
        assert str(request.url).endswith("/accounts/test-account/email/sending/send")
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["from"] == SENDER_EMAIL == "notifications@flats.spanashis.com"
        assert payload["to"] == recipient
        assert payload["subject"] == "2 new flats found"
        for body in (payload["html"], payload["text"]):
            assert "https://www.openrent.co.uk/2" in body
            assert "https://www.openrent.co.uk/3" in body
            assert all(f"https://www.openrent.co.uk/{n}" not in body for n in (1, 4, 99))
        assert running == 0
        # The provider sees only decisions committed before its POST starts.
        with ReviewDatabase(path) as db:
            assert [
                tuple(row)
                for row in db.connection.execute(
                    "SELECT property_id, status, decision FROM review.property_reviews "
                    "ORDER BY property_id"
                )
            ] == [
                (1, "complete", "reject"),
                (2, "complete", "pass"),
                (3, "complete", "uncertain"),
                (99, "complete", "pass"),
            ]
            stored = db.connection.execute(
                "SELECT json(result) FROM review.property_reviews WHERE property_id = 2"
            ).fetchone()[0]
            assert json.loads(stored) == judgement("pass").result
            assert (
                json.loads(
                    db.connection.execute(
                        "SELECT json(result) FROM review.property_reviews WHERE property_id = 3"
                    ).fetchone()[0]
                )
                == judgement("uncertain").result
            )
            assert db.counts()["emails_sending"] == 1
        return accepted(recipient)

    monkeypatch.setattr(review, "judge_property", judge)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-placeholder")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(post)) as client:
            monkeypatch.setattr(
                notifications,
                "CloudflareEmailSender",
                lambda configuration: CloudflareEmailSender(configuration, client=client),
            )
            assert await review.process_pending(cfg, quiet=True) == 0

    asyncio.run(run())
    assert sorted(judged) == [1, 2, 3] and len(requests) == 1 and maximum == 2
    with ReviewDatabase(path) as db:
        assert db.unprocessed_ids() == [4]
        assert [item["property_id"] for item in db.pending_notifications(recipient)] == [99]
        batch = db.connection.execute("SELECT * FROM review.email_batches").fetchone()
        assert (batch["status"], batch["attempts"], batch["provider_message_id"]) == (
            "sent",
            1,
            "test-provider-message",
        )
        assert [
            tuple(row)
            for row in db.connection.execute(
                "SELECT recipient, property_id FROM review.email_batch_items ORDER BY property_id"
            )
        ] == [(recipient, 2), (recipient, 3)]


def test_failed_review_and_empty_initial_cohort_do_not_send_or_reserve_email(tmp_path, monkeypatch):
    failed_path = tmp_path / "failed.sqlite"
    for property_id in (1, 2, 3):
        add_listing(failed_path, property_id)
    empty_path = tmp_path / "old-backlog.sqlite"
    saved_review(empty_path, 99)
    recipient = "self@example.com"
    provider = CloudflareEmailConfig("test-account", "test-placeholder", timeout=1)
    notification_config = notifications.NotificationConfig((recipient,), provider)
    judged = []

    async def judge(snapshot, images, criteria, **kwargs):
        judged.append(snapshot["id"])
        if snapshot["id"] == 2:
            raise review.JudgeError("mocked temporary failure")
        return judgement("pass" if snapshot["id"] == 1 else "uncertain")

    def forbidden(*args, **kwargs):
        raise AssertionError("An incomplete or empty review cycle must not start an email sender")

    monkeypatch.setattr(review, "judge_property", judge)
    monkeypatch.setattr(notifications, "CloudflareEmailSender", forbidden)
    cfg = replace(config(failed_path), notifications=notification_config)
    assert asyncio.run(review.process_pending(cfg, quiet=True)) == 1
    assert sorted(judged) == [1, 2, 3]
    with ReviewDatabase(failed_path) as db:
        assert db.unprocessed_ids() == [2]
        assert db.counts()["reviews_complete"] == 2 and db.counts()["reviews_error"] == 1
        assert db.counts()["email_batches"] == db.counts()["email_batch_items"] == 0
        assert [item["property_id"] for item in db.pending_notifications(recipient)] == [1, 3]
    empty_cfg = replace(config(empty_path), notifications=notification_config)
    assert asyncio.run(review.process_pending(empty_cfg, quiet=True)) == 0
    assert sorted(judged) == [1, 2, 3]
    with ReviewDatabase(empty_path) as db:
        assert db.unprocessed_ids() == []
        assert db.counts()["email_batches"] == db.counts()["email_batch_items"] == 0
        assert [item["property_id"] for item in db.pending_notifications(recipient)] == [99]


def test_recipient_isolation_frozen_retry_and_unknown_delivery_are_not_resent(
    tmp_path, monkeypatch
):
    path = tmp_path / "archive.sqlite"
    for property_id, decision in ((1, "pass"), (2, "reject"), (3, "uncertain")):
        saved_review(path, property_id, decision)
    first, second = "first@example.com", "second@example.com"
    timeout_recipient, cancelled_recipient = "timeout@example.com", "cancelled@example.com"
    phase = "reject"
    requests = []
    cancellation_started = asyncio.Event()
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-placeholder")

    async def post(request):
        payload = json.loads(request.content)
        requests.append(payload)
        recipient = payload["to"]
        assert "https://www.openrent.co.uk/2" not in payload["html"]
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
        async with httpx.AsyncClient(transport=httpx.MockTransport(post)) as client:
            monkeypatch.setattr(
                notifications,
                "CloudflareEmailSender",
                lambda configuration: CloudflareEmailSender(configuration, client=client),
            )
            assert await notifications.notify_command(notify_args(path, first.upper(), second)) == 1
            assert [item["to"] for item in requests] == [first, second]
            assert requests[0]["html"] == requests[1]["html"]
            assert requests[0]["subject"] == "2 new flats found"
            assert "https://www.openrent.co.uk/3" in requests[0]["html"]
            with ReviewDatabase(path) as db:
                assert db.counts()["emails_failed"] == db.counts()["emails_sent"] == 1
            # A retry uses the saved body even if the archive and pending set change.
            add_listing(path, 1, title="Changed after review", rent_pcm_pence=999_900)
            saved_review(path, 4)
            phase = "accept"
            assert await notifications.notify_command(notify_args(path, first, second)) == 0
            assert requests[2] == requests[0]
            assert [item["to"] for item in requests[2:]] == [first, first, second]
            for payload in requests[3:]:
                assert payload["subject"] == "1 new flats found"
                assert "https://www.openrent.co.uk/4" in payload["html"]
                assert "https://www.openrent.co.uk/1" not in payload["html"]
                assert "https://www.openrent.co.uk/3" not in payload["html"]
            assert await notifications.notify_command(notify_args(path, first, second)) == 0
            assert len(requests) == 5
            assert await notifications.notify_command(notify_args(path, timeout_recipient)) == 1
            assert len(requests) == 6
            assert await notifications.notify_command(notify_args(path, timeout_recipient)) == 1
            assert len(requests) == 6
            # Cancellation after the POST begins has the same uncertain acceptance rule.
            task = asyncio.create_task(
                notifications.notify_command(notify_args(path, cancelled_recipient))
            )
            await asyncio.wait_for(cancellation_started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(requests) == 7
            assert await notifications.notify_command(notify_args(path, cancelled_recipient)) == 1
            assert len(requests) == 7

    asyncio.run(run())
    with ReviewDatabase(path) as db:
        assert [
            tuple(row)
            for row in db.connection.execute(
                "SELECT recipient, status, attempts FROM review.email_batches ORDER BY id"
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
                for row in db.connection.execute(
                    "SELECT property_id FROM review.email_batch_items "
                    "WHERE recipient = ? ORDER BY property_id",
                    (recipient,),
                )
            ] == [1, 3, 4]
        assert not db.connection.execute("PRAGMA review.foreign_key_check").fetchall()


def test_cli_preview_needs_no_credentials_preserves_state_and_validates_options(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "archive.sqlite"
    for property_id, decision in ((1, "pass"), (2, "reject"), (3, "uncertain")):
        saved_review(path, property_id, decision)
    preview = tmp_path / "preview" / "digest.html"
    recipient = "self@example.com"
    criteria = tmp_path / "conditions.txt"
    criteria.write_text("My suitability conditions", encoding="utf-8")
    for name in (
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "RESEND_TOKEN",
        "OPENAI_API_KEY",
        "OPENRENT_REVIEW_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview and notify must not start a sender or model")

    monkeypatch.setattr(notifications, "CloudflareEmailSender", forbidden)
    monkeypatch.setattr(notifications, "ResendEmailSender", forbidden)
    monkeypatch.setattr(review, "judge_property", forbidden)
    runner = CliRunner()
    base = ["notify", "--db", str(path), "--email-provider", "cloudflare"]
    flags = ["--email-to", recipient, "--email-preview", str(preview)]
    result = runner.invoke(cli.app, [*base, *flags])
    assert result.exit_code == 0, result.output
    body = preview.read_text(encoding="utf-8")
    assert "https://www.openrent.co.uk/1" in body
    assert "https://www.openrent.co.uk/2" not in body
    assert "https://www.openrent.co.uk/3" in body
    result = runner.invoke(cli.app, [*base, *flags])
    assert result.exit_code == 0 and preview.read_text(encoding="utf-8") == body
    with ReviewDatabase(path) as db:
        assert db.counts()["reviews_complete"] == 3
        assert db.counts()["email_batches"] == db.counts()["email_batch_items"] == 0
        assert len(db.pending_notifications(recipient)) == 2
    result = runner.invoke(cli.app, [*base, "--email-to", recipient])
    assert result.exit_code == 1 and "CLOUDFLARE_API_TOKEN" in result.output
    result = runner.invoke(cli.app, ["notify", "--db", str(path), "--email-to", recipient])
    assert result.exit_code == 1 and "RESEND_TOKEN" in result.output
    result = runner.invoke(cli.app, [*base, "--email-preview", str(preview)])
    assert result.exit_code == 1 and "at least one --email-to" in result.output
    result = runner.invoke(cli.app, [*base, *flags, "--email-to", "another@example.com"])
    assert result.exit_code == 1 and "exactly one" in result.output
    result = runner.invoke(
        cli.app, [*base, "--email-to", "Name <self@example.com>", "--email-preview", str(preview)]
    )
    assert result.exit_code == 1 and "single plain email address" in result.output
    original_database = path.read_bytes()
    alias = tmp_path / "archive-alias.sqlite"
    alias.symlink_to(path)
    result = runner.invoke(cli.app, [*base, "--email-to", recipient, "--email-preview", str(alias)])
    assert result.exit_code == 1 and "SQLite database" in result.output
    assert path.read_bytes() == original_database
    review_flags = ["review", "--db", str(path), "--criteria-file", str(criteria), "--dry-run"]
    result = runner.invoke(
        cli.app,
        [*review_flags, "--email-to", recipient, "--email-preview", str(criteria)],
    )
    assert result.exit_code == 1 and "criteria file" in result.output
    assert criteria.read_text(encoding="utf-8") == "My suitability conditions"
    result = runner.invoke(
        cli.app, [*review_flags, "--stop-after-pass", "--review-concurrency", "2"]
    )
    assert result.exit_code == 1 and "requires --review-concurrency 1" in result.output
    result = runner.invoke(cli.app, [*review_flags, "--email-to", recipient, "--stop-after-pass"])
    assert result.exit_code == 1 and "finish all pending reviews" in result.output
    for token_file in (tmp_path / ".env", tmp_path / "private.env"):
        token_file.write_text("RESEND_TOKEN=test-placeholder\n", encoding="utf-8")
        env_flags = [] if token_file.name == ".env" else ["--env-file", str(token_file)]
        result = runner.invoke(
            cli.app,
            [*base, "--email-to", recipient, "--email-preview", str(token_file), *env_flags],
        )
        assert result.exit_code == 1 and "overwrite" in result.output
        assert token_file.read_text(encoding="utf-8") == "RESEND_TOKEN=test-placeholder\n"


def test_resend_cli_loads_token_files_env_wins_and_retries_saved_digest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RESEND_TOKEN", raising=False)
    path = tmp_path / "archive.sqlite"
    for property_id, decision in ((1, "pass"), (2, "reject"), (3, "uncertain")):
        saved_review(path, property_id, decision)
    token_file = tmp_path / "custom.env"
    token_file.write_text('RESEND_TOKEN="test-file-token"\n', encoding="utf-8")
    recipient = "self@example.com"
    requests = []
    headers = []
    batch_keys = []
    runner = CliRunner()
    flags = ["notify", "--db", str(path), "--email-to", recipient]

    async def post(request):
        assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
        payload = json.loads(request.content)
        requests.append(payload)
        headers.append(request.headers["Authorization"])
        batch_keys.append(request.headers["Idempotency-Key"])
        assert payload["from"] == SENDER_EMAIL
        assert payload["to"] == [recipient]
        assert payload["subject"] == (
            "2 new flats found" if len(requests) <= 2 else "1 new flats found"
        )
        assert "https://www.openrent.co.uk/2" not in payload["html"]
        if len(requests) <= 2:
            assert "https://www.openrent.co.uk/3" in payload["html"]
        else:
            assert "https://www.openrent.co.uk/3" not in payload["html"]
        with ReviewDatabase(path) as db:
            assert db.counts()["emails_sending"] == 1
            batch = db.connection.execute(
                "SELECT * FROM review.email_batches WHERE status='sending'"
            ).fetchone()
            assert batch["sender"] == SENDER_EMAIL
            assert notifications._batch_key(dict(batch)) != notifications._batch_key(
                {**dict(batch), "sender": "flats@spanashis.com"}
            )
        if len(requests) == 1:
            return httpx.Response(400, json={"message": "mocked definite rejection"})
        return httpx.Response(200, json={"id": f"00000000-0000-4000-8000-{len(requests):012d}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(post))
    monkeypatch.setattr(
        notifications,
        "ResendEmailSender",
        lambda configuration: ResendEmailSender(configuration, client=client),
    )
    try:
        # The default provider uses the explicitly chosen file without changing os.environ.
        result = runner.invoke(cli.app, [*flags, "--env-file", str(token_file)])
        assert result.exit_code == 1, result.output
        assert headers == ["Bearer test-file-token"]
        assert "RESEND_TOKEN" not in os.environ
        add_listing(path, 1, title="Changed after judging", rent_pcm_pence=999_900)
        saved_review(path, 4)
        monkeypatch.setenv("RESEND_TOKEN", "test-environment-token")
        result = runner.invoke(cli.app, [*flags, "--env-file", str(token_file)])
        assert result.exit_code == 0, result.output
        assert requests[1] == requests[0]
        assert batch_keys[1] == batch_keys[0]
        assert batch_keys[2] != batch_keys[0]
        assert headers[1:] == ["Bearer test-environment-token"] * 2
        assert "https://www.openrent.co.uk/4" in requests[2]["html"]
        assert "https://www.openrent.co.uk/1" not in requests[2]["html"]
        result = runner.invoke(cli.app, [*flags, "--env-file", str(token_file)])
        assert result.exit_code == 0 and len(requests) == 3
        # A normal run also discovers .env in the current working directory.
        saved_review(path, 5)
        monkeypatch.delenv("RESEND_TOKEN")
        (tmp_path / ".env").write_text("RESEND_TOKEN=test-default-file-token\n", encoding="utf-8")
        result = runner.invoke(cli.app, flags)
        assert result.exit_code == 0, result.output
        assert headers[-1] == "Bearer test-default-file-token"
        assert "https://www.openrent.co.uk/5" in requests[-1]["html"]
        assert len(requests) == 4
        assert len(set(batch_keys)) == 3
    finally:
        asyncio.run(client.aclose())
    with ReviewDatabase(path) as db:
        assert [
            tuple(row)
            for row in db.connection.execute(
                "SELECT status, attempts, provider_message_id FROM review.email_batches ORDER BY id"
            )
        ] == [
            ("sent", 2, "00000000-0000-4000-8000-000000000002"),
            ("sent", 1, "00000000-0000-4000-8000-000000000003"),
            ("sent", 1, "00000000-0000-4000-8000-000000000004"),
        ]
        assert [
            row[0]
            for row in db.connection.execute(
                "SELECT property_id FROM review.email_batch_items ORDER BY property_id"
            )
        ] == [1, 3, 4, 5]
