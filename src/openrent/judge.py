"""Judge archived listing evidence with the native asynchronous OpenAI Agents SDK."""

from __future__ import annotations

from .backends import (
    BackendConfig,
    BackendError,
    create_backend,
)
from .evidence import ReviewSnapshot
from .model_config import ModelConfig
from .review_models import JudgementOutput
from .review_prompt import render_listing


class JudgeError(ValueError):
    """The review model could not produce a complete, valid assessment."""


async def judge_property(
    evidence: ReviewSnapshot,
    criteria: str,
    *,
    model: str,
    backend: str = "codex",
    timeout: float = 300.0,
    model_config: ModelConfig | None = None,
) -> JudgementOutput:
    """Assess all archived evidence with the selected SDK backend."""
    instructions = f"""Review this flat against my preferences and tell me whether it is worth viewing.
Explain your reasons and anything important I should check. Use the listing
details and every supplied image, and search the web if it helps. Assess each named criterion
in the output schema and explain anything unknown.

My preferences:
{criteria}
"""
    config = BackendConfig(
        model=model,
        schema=JudgementOutput,
        instructions=instructions,
        timeout=timeout,
        model_config=model_config,
    )
    try:
        output = await create_backend(backend, config).run(
            render_listing(evidence.data, evidence.images), evidence.images
        )
    except BackendError as exc:
        raise JudgeError(str(exc)) from exc
    return output
