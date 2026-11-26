"""AWS CAPTCHA adapter and Gemini image selection, independent of alert delivery."""

import base64
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner
from awswaf.captcha import solve_captcha
from bs4 import BeautifulSoup
from pydantic import BaseModel, StrictInt

from .model_config import OpenRouterConfig
from .settings import load_settings

logger = logging.getLogger(__name__)


class CaptchaSolver(Protocol):
    async def solve(self, page, snapshot: dict) -> None:
        """Update the browser's verification state, or raise on failure."""
        ...


class ImageSelection(BaseModel):
    indices: list[StrictInt]


def validate_selection(indices, image_count):
    if (
        not isinstance(indices, list)
        or not indices
        or any(type(index) is not int or not 0 <= index < image_count for index in indices)
        or len(set(indices)) != len(indices)
    ):
        raise ValueError("Model returned invalid or duplicate image indices")
    return indices


class GeminiImageSelector:
    def __init__(self, config: OpenRouterConfig):
        self.config = config

    async def __call__(self, images, target):
        if not isinstance(images, list) or not 1 <= len(images) <= 16:
            raise ValueError("Unsupported CAPTCHA image count")
        if not isinstance(target, str) or not 1 <= len(target) <= 500:
            raise ValueError("Invalid CAPTCHA target")
        if any(not isinstance(image, str) for image in images):
            raise ValueError("Invalid CAPTCHA image data")
        if sum(map(len, images)) > 8 * 1024 * 1024:
            raise ValueError("CAPTCHA images exceed 8 MiB")
        content = [{"type": "input_text", "text": f"Select every image containing: {target}"}]
        for index, image in enumerate(images):
            base64.b64decode(image, validate=True)
            content.extend(
                [
                    {"type": "input_text", "text": f"Image index {index}"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image}",
                        "detail": "high",
                    },
                ]
            )
        async with self.config.create_client(timeout=25) as client:
            agent = Agent(
                name="CAPTCHA image selection",
                instructions="Identify matching images. Return their zero-based indices once each.",
                model=OpenAIResponsesModel(model=self.config.model, openai_client=client),
                output_type=ImageSelection,
                model_settings=replace(self.config.model_settings(), max_tokens=1024),
            )
            result = await Runner.run(
                agent,
                input=[{"role": "user", "content": content}],
                max_turns=1,
                run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
            )
        indices = validate_selection(result.final_output.indices, len(images))
        usage = result.context_wrapper.usage
        logger.info(
            "CAPTCHA image selection complete; model=%s images=%s input_tokens=%s output_tokens=%s",
            self.config.model,
            len(images),
            usage.input_tokens,
            usage.output_tokens,
        )
        return indices


def challenge_endpoints(html):
    """Only accept the AWS endpoints already embedded in the challenge page."""
    endpoints = {}
    for script in BeautifulSoup(html, "html.parser").find_all("script", src=True):
        url = urlsplit(script["src"])
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.query
            or url.fragment
        ):
            continue
        for kind in ("token", "captcha"):
            filename = "challenge.js" if kind == "token" else "captcha.js"
            if (
                url.hostname
                and url.hostname.endswith(f".{kind}.awswaf.com")
                and url.path.endswith("/" + filename)
            ):
                endpoints[kind] = script["src"].rsplit("/", 1)[0]
    if set(endpoints) != {"token", "captcha"}:
        raise ValueError("Page does not contain supported AWS CAPTCHA endpoints")
    return endpoints


class AwsWafCaptchaSolver:
    def __init__(self, select_images):
        self.select_images = select_images

    async def solve(self, page, snapshot):
        endpoints = challenge_endpoints(snapshot["body"])
        props = await page.evaluate("() => window.gokuProps")
        if not isinstance(props, dict) or not all(props.get(k) for k in ("key", "iv", "context")):
            raise ValueError("AWS challenge parameters are missing")
        origin = "https://" + urlsplit(page.url).netloc
        cookies = await page.context.cookies(page.url)
        token = next((c for c in cookies if c["name"] == "aws-waf-token"), None)
        if token is None:
            raise ValueError("Browser has not completed the silent AWS challenge")

        async def request(method, url, data):
            response = await page.request.fetch(
                url,
                method=method,
                data=json.dumps(data) if data is not None else None,
                headers={
                    "origin": origin,
                    "referer": page.url,
                    "content-type": "text/plain;charset=UTF-8",
                },
                timeout=10000,
                max_redirects=0,
            )
            try:
                if not response.ok:
                    raise ValueError(f"AWS CAPTCHA request failed with HTTP {response.status}")
                return await response.json()
            finally:
                await response.dispose()

        verified = await solve_captcha(
            request=request,
            select_images=self.select_images,
            captcha_url=endpoints["captcha"],
            token_url=endpoints["token"],
            domain=urlsplit(page.url).hostname,
            goku_props=props,
            existing_token=token["value"],
        )
        await page.context.add_cookies([token | {"value": verified}])


def configuration(environ):
    settings = load_settings(Path(environ.get("ENV_FILE", ".env")).resolve())
    if not settings.openrouter_api_key.strip():
        raise ValueError("Set OPENROUTER_API_KEY or OPENROUTER_TOKEN for CAPTCHA solving.")
    config = OpenRouterConfig(
        environ.get("BROWSER_CAPTCHA_MODEL", "google/gemini-3.8-flash"),
        settings.openrouter_api_key,
        environ.get("BROWSER_CAPTCHA_PROVIDER") or None,
    )
    return AwsWafCaptchaSolver(GeminiImageSelector(config))
