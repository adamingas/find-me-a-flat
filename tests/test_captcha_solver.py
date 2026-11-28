import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("awswaf")

from awswaf.captcha import SelectionRejected, TokenExchangeFailed, solve_captcha

from openrent.browser_worker import BrowserConfig, BrowserWorker
from openrent.captcha_solver import (
    AwsWafCaptchaSolver,
    RetryingCaptchaSolver,
    challenge_endpoints,
    configuration,
    validate_selection,
)


def test_configured_model_escalation(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    solver = configuration({"ENV_FILE": str(tmp_path / ".env")})
    assert [(s.select_images.config.model, s.select_images.detail) for s in solver.attempts] == [
        ("google/gemini-3.1-flash-lite", "low"),
        ("google/gemini-3.1-flash-lite", "low"),
        ("google/gemini-3.8-flash", "high"),
    ]


@pytest.mark.parametrize("indices", [[-1], [3], [True], [0, 0], [0.5], ["0"], [], None])
def test_rejects_invalid_model_indices(indices):
    with pytest.raises(ValueError):
        validate_selection(indices, 3)


def test_valid_selection_and_challenge_host_validation():
    assert validate_selection([2, 0], 3) == [2, 0]
    html = """<script src="https://tenant.token.awswaf.com/a/challenge.js"></script>
    <script src="https://tenant.captcha.awswaf.com/a/captcha.js"></script>"""
    assert challenge_endpoints(html) == {
        "token": "https://tenant.token.awswaf.com/a",
        "captcha": "https://tenant.captcha.awswaf.com/a",
    }
    with pytest.raises(ValueError):
        challenge_endpoints(html.replace("captcha.awswaf.com", "captcha.awswaf.com.evil.test"))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "missing_voucher", "missing_token"])
async def test_captcha_exchange_only_redeems_an_accepted_solution(outcome):
    calls = []
    problem = {
        "state": {"iv": "opaque", "payload": "opaque"},
        "key": "key",
        "hmac_tag": "tag",
        "assets": {"images": json.dumps([base64.b64encode(b"image").decode()])},
        "localized_assets": {"target0": "bus"},
    }

    async def request(method, url, data):
        calls.append((method, url, data))
        if "/problem?" in url:
            return problem
        if url.endswith("/verify"):
            assert data["client_solution"] == [0]
            assert data["goku_props"] == {"key": "current"}
            assert data["state"] == problem["state"]
            return {
                "success": outcome != "rejected",
                "captcha_voucher": "" if outcome == "missing_voucher" else "voucher",
            }
        assert data == {"captcha_voucher": "voucher", "existing_token": "browser-token"}
        return {"token": "" if outcome == "missing_token" else "verified-token"}

    async def select(images, target):
        assert target == "bus"
        assert len(images) == 1
        return [0]

    call = solve_captcha(
        request=request,
        select_images=select,
        captcha_url="https://captcha",
        token_url="https://token",
        domain="www.openrent.co.uk",
        goku_props={"key": "current"},
        existing_token="browser-token",
    )
    if outcome == "accepted":
        assert await call == "verified-token"
        assert len(calls) == 3
    else:
        error = SelectionRejected if outcome == "rejected" else TokenExchangeFailed
        with pytest.raises(error):
            await call
        assert len(calls) == (3 if outcome == "missing_token" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [0, 1, 2, 3])
async def test_retry_order_stops_on_success_and_never_exceeds_three_attempts(failures, caplog):
    calls = []

    class Solver:
        def __init__(self, name):
            self.name = name

        async def solve(self, page, snapshot):
            calls.append((self.name, page, snapshot))
            if len(calls) <= failures:
                raise SelectionRejected("secret-token")

    current, fallback = Solver("current"), Solver("fallback")
    solver = RetryingCaptchaSolver((current, current, fallback))
    page, snapshot = object(), {"body": "challenge"}
    caplog.set_level(logging.INFO, logger="openrent.captcha_solver")
    if failures == 3:
        with pytest.raises(SelectionRejected):
            await solver.solve(page, snapshot)
    else:
        await solver.solve(page, snapshot)
    expected = ["current", "current", "fallback"][: min(failures + 1, 3)]
    assert calls == [(name, page, snapshot) for name in expected]
    assert caplog.text.count("CAPTCHA attempt failed") == failures
    assert caplog.text.count("CAPTCHA attempt succeeded") == (failures < 3)
    assert "secret-token" not in caplog.text
    if failures:
        assert "error=SelectionRejected" in caplog.text


@pytest.mark.asyncio
async def test_timeout_cancels_attempt_before_starting_next_one():
    cancelled = asyncio.Event()

    class HangingSolver:
        async def solve(self, page, snapshot):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class Fallback:
        async def solve(self, page, snapshot):
            assert cancelled.is_set()

    await RetryingCaptchaSolver((HangingSolver(), Fallback()), attempt_timeout=0.01).solve(None, {})


@pytest.mark.asyncio
async def test_cancellation_does_not_start_retry():
    entered = asyncio.Event()
    calls = []

    class Solver:
        async def solve(self, page, snapshot):
            calls.append("solve")
            entered.set()
            await asyncio.Event().wait()

    solver = Solver()
    task = asyncio.create_task(RetryingCaptchaSolver((solver, solver, solver)).solve(None, {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["solve"]


@pytest.mark.asyncio
async def test_aws_retries_fetch_fresh_puzzles_and_only_store_verified_cookie():
    states = []
    token = {"name": "aws-waf-token", "value": "old", "domain": "www.openrent.co.uk"}
    context = SimpleNamespace(cookies=AsyncMock(return_value=[token]), add_cookies=AsyncMock())

    async def fetch(url, *, method, data, **kwargs):
        context.add_cookies.assert_not_awaited()
        if "/problem?" in url:
            result = {
                "state": f"puzzle-{len(states) + 1}",
                "key": "key",
                "hmac_tag": "tag",
                "assets": {"images": json.dumps(["aW1hZ2U="])},
                "localized_assets": {"target0": "bus"},
            }
        elif url.endswith("/verify"):
            states.append(json.loads(data)["state"])
            result = {"success": len(states) == 3, "captcha_voucher": "voucher"}
        else:
            assert url.endswith("/voucher")
            result = {"token": "verified"}
        return SimpleNamespace(ok=True, json=AsyncMock(return_value=result), dispose=AsyncMock())

    page = SimpleNamespace(
        url="https://www.openrent.co.uk/search",
        context=context,
        evaluate=AsyncMock(return_value={"key": "key", "iv": "iv", "context": "context"}),
        request=SimpleNamespace(fetch=fetch),
    )
    snapshot = {
        "body": """
        <script src="https://tenant.token.awswaf.com/a/challenge.js"></script>
        <script src="https://tenant.captcha.awswaf.com/a/captcha.js"></script>
    """
    }
    current, fallback = AsyncMock(return_value=[0]), AsyncMock(return_value=[0])
    primary = AwsWafCaptchaSolver(current)
    await RetryingCaptchaSolver((primary, primary, AwsWafCaptchaSolver(fallback))).solve(
        page, snapshot
    )
    assert states == ["puzzle-1", "puzzle-2", "puzzle-3"]
    assert current.await_count == 2
    assert fallback.await_count == 1
    context.add_cookies.assert_awaited_once_with([token | {"value": "verified"}])


@pytest.mark.asyncio
async def test_failed_retry_sequence_runs_once_per_episode_and_keeps_secrets_out_of_logs(
    tmp_path, caplog
):
    calls = []

    class Solver:
        async def solve(self, page, snapshot):
            calls.append(snapshot)
            raise ValueError("secret-token and private model response")

    solver = Solver()
    worker = BrowserWorker(
        BrowserConfig(tmp_path, tmp_path / "password"),
        RetryingCaptchaSolver((solver, solver, solver)),
    )
    worker.episode = "episode-one"
    deadline = asyncio.get_running_loop().time() + 30
    caplog.set_level(logging.INFO, logger="openrent.browser_worker")
    assert not await worker._try_solver({}, deadline)
    assert not await worker._try_solver({}, deadline)
    assert len(calls) == 3
    assert "handing off to human" in caplog.text
    assert "secret-token" not in caplog.text
    worker.episode = "episode-two"
    assert not await worker._try_solver({}, deadline)
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_solver_cancellation_propagates_and_leaves_human_episode(tmp_path):
    entered = asyncio.Event()

    class Solver:
        async def solve(self, page, snapshot):
            entered.set()
            await asyncio.Event().wait()

    worker = BrowserWorker(BrowserConfig(tmp_path, tmp_path / "password"), Solver())
    worker.episode = "episode"
    task = asyncio.create_task(worker._try_solver({}, asyncio.get_running_loop().time() + 30))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.episode == "episode"
    assert not await worker._try_solver({}, asyncio.get_running_loop().time() + 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["solved", "rejected", "ineffective"])
async def test_worker_checks_original_response_before_declaring_solver_success(tmp_path, outcome):
    calls = []
    blocked = {
        "url": "https://www.openrent.co.uk/search",
        "status": 405,
        "headers": {"x-amzn-waf-action": "captcha"},
        "body": b"challenge",
    }
    normal = blocked | {"status": 200, "headers": {}, "body": b"listing results"}

    class Page:
        async def set_extra_http_headers(self, headers):
            pass

        async def goto(self, url, **kwargs):
            calls.append("navigate")

        async def reload(self, **kwargs):
            calls.append("reload")

    class Solver:
        async def solve(self, page, snapshot):
            calls.append("solve")
            if outcome == "solved":
                worker.current = normal
            elif outcome == "rejected":
                raise ValueError("rejected")

    class Worker(BrowserWorker):
        async def _ensure_browser(self):
            self.page = Page()
            self.response = object()

        async def _snapshot(self):
            return self.current

    worker = Worker(BrowserConfig(tmp_path, tmp_path / "password", settle_seconds=0.001), Solver())
    worker.current = blocked
    result = await worker.fetch(blocked["url"])
    assert calls.count("solve") == 1
    if outcome == "solved":
        assert result["body"] == b"listing results"
        assert worker.episode is None
    else:
        assert result == {"episode": worker.episode}
        worker.response = object()
        assert await worker.fetch(blocked["url"]) == result
        assert calls.count("solve") == 1
        assert calls.count("navigate") == 1


@pytest.mark.asyncio
async def test_solver_timeout_returns_to_human_without_retrying(tmp_path):
    cancelled = asyncio.Event()

    class Solver:
        async def solve(self, page, snapshot):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker = BrowserWorker(BrowserConfig(tmp_path, tmp_path / "password"), Solver())
    worker.episode = "episode"
    deadline = asyncio.get_running_loop().time() + 5.02
    assert not await worker._try_solver({}, deadline)
    assert cancelled.is_set()
    assert not await worker._try_solver({}, deadline + 30)
    assert worker.episode == "episode"
