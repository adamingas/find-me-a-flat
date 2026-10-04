import asyncio
import json

import httpx
import pytest

from openrent.email_sender import (
    EmailSendError,
    EmailSendIndeterminate,
    ResendEmailConfig,
    ResendEmailSender,
    validate_recipient,
)


def test_sender_validates_inputs_before_any_request():
    for address in (
        "one@example.com,two@example.com",
        "Reviewer <one@example.com>",
        "one@example.com\r\nBcc: two@example.com",
        ".one@example.com",
        "one..two@example.com",
        "one@example..com",
    ):
        with pytest.raises(ValueError):
            validate_recipient(address)
    assert validate_recipient("reviewer+flats@spanashis.com") == "reviewer+flats@spanashis.com"
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            ResendEmailConfig("secret", timeout)
    with pytest.raises(ValueError):
        ResendEmailConfig("secret\nInjected")

    async def handler(request):
        pytest.fail("Invalid input must not reach Resend.")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resend = ResendEmailSender(ResendEmailConfig("secret"), client=client)
            with pytest.raises(ValueError):
                await resend.send(
                    "reviewer@example.com", "Subject\r\nBcc: other@example.com", "A", "A"
                )
            with pytest.raises(ValueError):
                await resend.send("reviewer@example.com", "1 new flats found", "", "")
            for key in ("", "a" * 257, "key\r\nInjected: value"):
                with pytest.raises(ValueError):
                    await resend.send(
                        "reviewer@example.com",
                        "1 new flats found",
                        "Flat",
                        "Flat",
                        idempotency_key=key,
                    )

    asyncio.run(run())


def test_resend_sends_expected_payload_headers_and_acceptance_receipt():
    requests = []
    message_id = "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"
    config = ResendEmailConfig("secret-resend-token-never-sent-to-a-server", timeout=9)

    async def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.resend.com/emails"
        assert (
            request.headers["Authorization"] == "Bearer secret-resend-token-never-sent-to-a-server"
        )
        assert request.headers["User-Agent"] == "openrent-fetch/0.1"
        if len(requests) == 1:
            assert request.headers["Idempotency-Key"] == "flat-digest/durable-batch-123"
        else:
            assert "Idempotency-Key" not in request.headers
        assert all(timeout == 9 for timeout in request.extensions["timeout"].values())
        assert json.loads(request.content) == {
            "from": "notifications@flats.spanashis.com",
            "to": ["reviewer@example.com"],
            "subject": "2 new flats found",
            "html": "<p>Two flats.</p>",
            "text": "Two flats.",
        }
        return httpx.Response(200, json={"id": message_id})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            sender = ResendEmailSender(config, client=client)
            for key in ("flat-digest/durable-batch-123", None):
                result = await sender.send(
                    "reviewer@example.com",
                    "2 new flats found",
                    "<p>Two flats.</p>",
                    "Two flats.",
                    idempotency_key=key,
                )
                assert result.accepted is True
                assert result.provider_message_id == message_id

    asyncio.run(run())
    assert len(requests) == 2
    assert "secret-resend-token" not in repr(config)


def test_resend_rejections_remain_retryable_without_leaking_provider_messages():
    statuses = iter((400, 401, 403, 422, 429))
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            next(statuses),
            json={"name": "validation_error", "message": "secret-resend-token"},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            sender = ResendEmailSender(ResendEmailConfig("secret-resend-token"), client=client)
            for _ in range(5):
                with pytest.raises(EmailSendError) as error:
                    await sender.send("reviewer@example.com", "1 new flats found", "Flat", "Flat")
                assert not isinstance(error.value, EmailSendIndeterminate)
                assert "secret-resend-token" not in str(error.value)

    asyncio.run(run())
    assert len(requests) == 5


def test_resend_keeps_network_conflicts_and_unclear_receipts_indeterminate():
    outcomes = iter(
        (
            "timeout",
            httpx.Response(409, json={"name": "concurrent_idempotent_requests"}),
            httpx.Response(503, text="secret-resend-token"),
            httpx.Response(200, json={"id": None}),
            httpx.Response(200, text="invalid receipt"),
            httpx.Response(302, headers={"Location": "https://other.invalid/send"}),
        )
    )
    requests = []

    async def handler(request):
        requests.append(request)
        outcome = next(outcomes)
        if outcome == "timeout":
            raise httpx.ReadTimeout("secret-resend-token", request=request)
        return outcome

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            sender = ResendEmailSender(ResendEmailConfig("secret-resend-token"), client=client)
            for _ in range(6):
                with pytest.raises(EmailSendIndeterminate) as error:
                    await sender.send(
                        "reviewer@example.com",
                        "1 new flats found",
                        "Flat",
                        "Flat",
                        idempotency_key="flat-digest/123",
                    )
                assert "secret-resend-token" not in str(error.value)
                assert error.value.__suppress_context__ or error.value.__context__ is None

    asyncio.run(run())
    assert len(requests) == 6
