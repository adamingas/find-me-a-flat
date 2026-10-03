"""Shared, typed interface for asynchronous model-backed assessments."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agents import AgentOutputSchema
from pydantic import BaseModel, ValidationError


class BackendError(ValueError):
    """A backend could not produce a valid assessment."""


class ImageEvidence(Protocol):
    content: bytes
    content_type: str


@dataclass(frozen=True)
class BackendConfig:
    model: str
    schema: type[BaseModel]
    instructions: str
    timeout: float = 300.0

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("An explicit model name is required.")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("Instructions must contain nonblank text.")
        if not isinstance(self.schema, type) or not issubclass(self.schema, BaseModel):
            raise TypeError("Schema must be a Pydantic model class.")
        if strict_schema(self.schema).get("type") != "object":
            raise ValueError("Schema must describe a JSON object Pydantic model.")
        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be positive and finite.")


class AgentBackend(Protocol):
    config: BackendConfig

    async def run(self, data: str | dict, images: Sequence[ImageEvidence]) -> BaseModel: ...


def image_label(image: ImageEvidence, number: int, total: int) -> str:
    """Number gallery evidence and preserve any supplied kind and caption."""
    label = f"Image {number} of {total}"
    if kind := getattr(image, "kind", None):
        label += f" ({kind})"
    caption = getattr(image, "caption", None)
    return f"{label}: {caption}" if caption else f"{label}:"


def strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    return AgentOutputSchema(schema, strict_json_schema=True).json_schema()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def parse_output(raw: str, schema: type[BaseModel]) -> BaseModel:
    try:
        json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        return schema.model_validate_json(raw, strict=True, extra="forbid")
    except (ValueError, TypeError, RecursionError, ValidationError) as exc:
        raise BackendError("The model returned invalid structured output.") from exc


class StructuredOutput(AgentOutputSchema):
    """Use the same strict JSON validation for API and Codex responses."""

    def validate_json(self, json_str: str) -> BaseModel:
        return parse_output(json_str, self.output_type)


def create_backend(name: str, config: BackendConfig) -> AgentBackend:
    if name == "codex":
        from .codex_backend import CodexBackend

        return CodexBackend(config)
    if name == "responses":
        from .responses_backend import ResponsesBackend

        return ResponsesBackend(config)
    raise ValueError(f"Unknown review backend: {name}")
