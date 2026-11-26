import asyncio
import base64
import json
import logging

import pytest

pytest.importorskip("awswaf")

from awswaf.captcha import CaptchaRejected, solve_captcha

from openrent.browser_worker import BrowserConfig, BrowserWorker
from openrent.captcha_solver import challenge_endpoints, validate_selection


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
@pytest.mark.parametrize("accepted", [True, False])
async def test_captcha_exchange_only_redeems_an_accepted_solution(accepted):
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
            return {"success": accepted, "captcha_voucher": "voucher"}
        assert data == {"captcha_voucher": "voucher", "existing_token": "browser-token"}
        return {"token": "verified-token"}

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
    if accepted:
        assert await call == "verified-token"
        assert len(calls) == 3
    else:
        with pytest.raises(CaptchaRejected):
            await call
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_solver_runs_once_per_episode_and_keeps_secrets_out_of_logs(tmp_path, caplog):
    calls = []

    class Solver:
        async def solve(self, page, snapshot):
            calls.append(snapshot)
            raise ValueError("secret-token and private model response")

    worker = BrowserWorker(BrowserConfig(tmp_path, tmp_path / "password"), Solver())
    worker.episode = "episode-one"
    deadline = asyncio.get_running_loop().time() + 30
    caplog.set_level(logging.INFO, logger="openrent.browser_worker")
    assert not await worker._try_solver({}, deadline)
    assert not await worker._try_solver({}, deadline)
    assert len(calls) == 1
    assert "handing off to human" in caplog.text
    assert "secret-token" not in caplog.text
    worker.episode = "episode-two"
    assert not await worker._try_solver({}, deadline)
    assert len(calls) == 2


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
