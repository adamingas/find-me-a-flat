"""Judge archived listing evidence with the native asynchronous OpenAI Agents SDK."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .backends import (
    BackendConfig,
    BackendError,
    ImageEvidence,
    create_backend,
    parse_output,
    strict_schema,
)
from .review_models import JudgementOutput
from .review_prompt import render_listing


class JudgeError(ValueError):
    """The review model could not produce a complete, valid assessment."""


Judgement = JudgementOutput


def judgement_schema() -> dict[str, Any]:
    """Return exactly the JSON Schema used by the Agents structured output contract."""
    return strict_schema(JudgementOutput)


def _validate_judgement(result: JudgementOutput, image_count: int) -> Judgement:
    if image_count <= 0:
        raise JudgeError("Review cannot complete with missing photographs")
    return result


def _parse_judgement(raw: str, image_count: int) -> Judgement:
    """Apply the shared structured-output validation and rental decision rules."""
    try:
        result = parse_output(raw, JudgementOutput)
    except BackendError as exc:
        raise JudgeError("Review returned invalid structured JSON") from exc
    return _validate_judgement(result, image_count)


async def judge_property(
    snapshot: dict,
    images: Sequence[ImageEvidence],
    criteria: str,
    *,
    model: str,
    backend: str = "codex",
    timeout: float = 300.0,
) -> Judgement:
    """Assess all archived evidence with the selected SDK backend."""
    if not isinstance(criteria, str) or not criteria.strip() or len(criteria) > 64000:
        raise ValueError("criteria must contain between 1 and 64000 characters")
    if not images:
        raise JudgeError("Review cannot complete with missing photographs")
    instructions = f"""Review this flat against my preferences and tell me whether it is worth viewing.
Explain your reasons and anything important I should check. Use the listing
details and every supplied image, and search the web if it helps. Assess each named criterion
in the output schema and explain anything unknown.

My preferences:
{criteria}
"""
    config = BackendConfig(
        model=model, schema=JudgementOutput, instructions=instructions, timeout=timeout
    )
    try:
        output = await create_backend(backend, config).run(render_listing(snapshot, images), images)
    except BackendError as exc:
        raise JudgeError(str(exc)) from exc
    if not isinstance(output, JudgementOutput):
        raise JudgeError("Review did not return the required structured assessment")
    return _validate_judgement(output, len(images))
