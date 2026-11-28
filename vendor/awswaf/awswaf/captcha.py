"""Async Python port of xKiian/awswaf's Go CAPTCHA protocol.

HTTP and image recognition are supplied by the caller; no model credentials,
network clients, retries, or browser ownership live in this dependency.
"""

import json
import time
from urllib.parse import urlencode


class CaptchaRejected(ValueError):
    pass


class SelectionRejected(CaptchaRejected):
    """AWS rejected the selected images."""


class TokenExchangeFailed(CaptchaRejected):
    """AWS accepted the selection but did not issue a verification token."""


async def solve_captcha(*, request, select_images, captcha_url, token_url, domain,
                        goku_props, existing_token):
    problem = await request(
        "GET", captcha_url + "/problem?" + urlencode(
            {"kind": "visual", "domain": domain, "locale": "en"}
        ), None,
    )
    images = json.loads(problem["assets"]["images"])
    target = problem["localized_assets"]["target0"]
    started = time.monotonic()
    solution = await select_images(images, target)
    result = await request("POST", captcha_url + "/verify", {
        "state": problem["state"],
        "key": problem["key"],
        "hmac_tag": problem["hmac_tag"],
        "client_solution": solution,
        "metrics": {"solve_time_millis": int((time.monotonic() - started) * 1000)},
        "goku_props": goku_props,
        "locale": "en",
    })
    if result.get("success") is not True:
        raise SelectionRejected("AWS rejected the image selection")
    voucher = result.get("captcha_voucher")
    if not isinstance(voucher, str) or not voucher:
        raise TokenExchangeFailed("AWS did not return a CAPTCHA voucher")
    result = await request("POST", token_url + "/voucher", {
        "captcha_voucher": voucher,
        "existing_token": existing_token,
    })
    token = result.get("token")
    if not isinstance(token, str) or not token:
        raise TokenExchangeFailed("AWS did not return a verified token")
    return token
