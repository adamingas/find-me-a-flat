"""Native asynchronous Agents SDK backend using the OpenAI Responses API."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from typing import Any

from agents import (
    Agent,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    WebSearchTool,
)
from agents.exceptions import AgentsException
from openai import APIStatusError, AuthenticationError, OpenAIError
from pydantic import BaseModel, ValidationError

from .backends import BackendConfig, BackendError, ImageEvidence, StructuredOutput, image_label
from .model_config import OpenAIConfig


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

    ModelConfig supplies the client credentials, endpoint and request settings. Archived image bytes
    become inline input_image data URLs, never paths or files. The model can use
    hosted web search but has no shell, filesystem, connector or handoff tools.
    """

    def __init__(self, config: BackendConfig):
        self.config = config

    async def run(self, data: str, images: Sequence[ImageEvidence]) -> BaseModel:
        config = self.config
        evidence = data
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
                        "text": image_label(image, number, len(images)),
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
            model_config = config.model_config or OpenAIConfig(config.model)
            client = model_config.create_client(timeout=config.timeout)
            # OpenRouter's Responses adapter rejects the SDK's null location.
            # An empty approximate location preserves search without inventing a city.
            search_options = (
                {"user_location": {"type": "approximate"}}
                if client.base_url.host == "openrouter.ai"
                else {}
            )
            agent = Agent(
                name="Property review",
                instructions=config.instructions,
                model=OpenAIResponsesModel(model=config.model, openai_client=client),
                tools=[WebSearchTool(external_web_access=True, **search_options)],
                handoffs=[],
                output_type=StructuredOutput(config.schema, strict_json_schema=True),
                model_settings=model_config.model_settings(),
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
                "Model API authentication failed; check the selected API credentials"
            ) from None
        except APIStatusError as exc:
            raise BackendError(f"Model review request failed with HTTP {exc.status_code}") from None
        except OpenAIError:
            raise BackendError(
                "OpenAI review request failed; check API configuration and availability"
            ) from None
        except (AgentsException, ValidationError):
            raise BackendError("Responses review did not return valid structured output") from None
        finally:
            if client is not None:
                await _finish_cleanup(asyncio.create_task(client.close()))
