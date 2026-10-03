"""Native asynchronous Agents SDK backend using the OpenAI Responses API."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    WebSearchTool,
)
from agents.exceptions import AgentsException
from openai import APIStatusError, AsyncOpenAI, AuthenticationError, OpenAIError
from pydantic import BaseModel, ValidationError

from .backends import BackendConfig, BackendError, ImageEvidence, StructuredOutput, image_label


async def _finish_cleanup(task: asyncio.Task) -> None:
    """Close the HTTP client before propagating repeated cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class ResponsesBackend:
    """Send all evidence in one model context with strict output and web search.

    The OpenAI client authenticates using OPENAI_API_KEY. Archived image bytes
    become inline input_image data URLs, never paths or files. The model can use
    hosted web search but has no shell, filesystem, connector or handoff tools.
    """

    def __init__(self, config: BackendConfig):
        self.config = config

    async def run(self, data: str | dict, images: Sequence[ImageEvidence]) -> BaseModel:
        config = self.config
        if isinstance(data, str):
            evidence = data
        elif isinstance(data, dict):
            try:
                metadata = json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True)
            except (TypeError, ValueError, RecursionError) as exc:
                raise BackendError(
                    "Review data must be JSON-serializable with finite values"
                ) from exc
            evidence = f"UNTRUSTED LISTING DATA (JSON):\n{metadata}"
        else:
            raise BackendError("Review data must be text or a dictionary")
        if len(evidence.encode()) + len(config.instructions.encode()) > 8 * 1024 * 1024:
            raise BackendError("Review data and instructions exceed 8 MiB; nothing was truncated")

        content: list[dict[str, Any]] = [{"type": "input_text", "text": evidence}]
        for number, image in enumerate(images, 1):
            if image.content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise BackendError("Review images must be JPEG, PNG, WebP or GIF")
            if not isinstance(image.content, bytes) or not image.content:
                raise BackendError("Every review image must contain nonempty archived bytes")
            encoded = base64.b64encode(image.content).decode("ascii")
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": image_label(image, number, len(images))
                        if isinstance(data, str)
                        else f"Image {number} of {len(images)}:",
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:{image.content_type};base64,{encoded}",
                        "detail": "high",
                    },
                ]
            )

        client = None
        try:
            client = AsyncOpenAI(timeout=config.timeout, max_retries=0)
            agent = Agent(
                name="Property review",
                instructions=config.instructions,
                model=OpenAIResponsesModel(model=config.model, openai_client=client),
                tools=[WebSearchTool(external_web_access=True)],
                handoffs=[],
                output_type=StructuredOutput(config.schema, strict_json_schema=True),
                model_settings=ModelSettings(truncation="disabled", store=False),
            )
            async with asyncio.timeout(config.timeout):
                result = await Runner.run(
                    agent,
                    input=[{"role": "user", "content": content}],
                    max_turns=3,
                    run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
                )
            if not isinstance(result.final_output, config.schema):
                raise BackendError("Review did not return the required structured result")
            return result.final_output
        except TimeoutError:
            raise BackendError(
                f"Responses review timed out after {config.timeout:g} seconds"
            ) from None
        except AuthenticationError:
            raise BackendError(
                "OpenAI API authentication failed; configure OPENAI_API_KEY"
            ) from None
        except APIStatusError as exc:
            raise BackendError(
                f"OpenAI review request failed with HTTP {exc.status_code}"
            ) from None
        except OpenAIError:
            raise BackendError(
                "OpenAI review request failed; check API configuration and availability"
            ) from None
        except (AgentsException, ValidationError):
            raise BackendError("Responses review did not return valid structured output") from None
        finally:
            if client is not None:
                await _finish_cleanup(asyncio.create_task(client.close()))
