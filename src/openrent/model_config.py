"""Model API credentials, routing and capabilities, independent of review criteria."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from agents import ModelSettings
from openai import AsyncOpenAI


class ModelConfig(Protocol):
    model: str

    def create_client(self, *, timeout: float) -> AsyncOpenAI: ...

    def model_settings(self) -> ModelSettings: ...

    def audit_identity(self) -> dict[str, Any]: ...

    async def validate(self, *, images: bool, structured_output: bool) -> None: ...


@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    api_key: str | None = field(default=None, repr=False, compare=False)

    def create_client(self, *, timeout: float) -> AsyncOpenAI:
        credentials = {"api_key": self.api_key} if self.api_key is not None else {}
        return AsyncOpenAI(
            base_url="https://api.openai.com/v1", timeout=timeout, max_retries=0, **credentials
        )

    def model_settings(self) -> ModelSettings:
        return ModelSettings(truncation="disabled", store=False, preserve_raw_usage=True)

    def audit_identity(self) -> dict[str, Any]:
        return {"api": "openai", "model": self.model}

    async def validate(self, *, images: bool, structured_output: bool) -> None:
        return None


@dataclass(frozen=True)
class OpenRouterConfig:
    model: str
    api_key: str = field(repr=False, compare=False)
    provider: str | None = None

    def __post_init__(self):
        if not self.model.strip():
            raise ValueError("An explicit OpenRouter model is required.")
        if self.provider is not None and not self.provider.strip():
            raise ValueError("OpenRouter provider must be a nonblank provider slug.")
        if self.provider is not None:
            object.__setattr__(self, "provider", self.provider.strip().casefold())

    def create_client(self, *, timeout: float) -> AsyncOpenAI:
        if not self.api_key.strip():
            raise ValueError("Set OPENROUTER_API_KEY or OPENROUTER_TOKEN.")
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=timeout,
            max_retries=0,
        )

    def model_settings(self) -> ModelSettings:
        routing: dict[str, Any] = {"require_parameters": True}
        if self.provider is not None:
            routing.update(only=[self.provider], allow_fallbacks=False)
        return ModelSettings(
            truncation="disabled",
            store=False,
            preserve_raw_usage=True,
            extra_body={"provider": routing},
        )

    def audit_identity(self) -> dict[str, Any]:
        return {"api": "openrouter", "model": self.model, "provider": self.provider}

    async def validate(self, *, images: bool, structured_output: bool) -> None:
        """Check advertised capabilities before claiming or reviewing stored evidence."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models/"
                    + quote(self.model, safe="/")
                    + "/endpoints"
                )
                response.raise_for_status()
                data = response.json()["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ValueError("Could not verify OpenRouter model/provider capabilities.") from None
        if images and "image" not in data.get("architecture", {}).get("input_modalities", []):
            raise ValueError(f"OpenRouter model {self.model} does not advertise image input.")
        endpoints = data.get("endpoints", [])
        if self.provider is not None:
            endpoints = [
                item
                for item in endpoints
                if item.get("tag", "").casefold().split("/")[0] == self.provider
                or item.get("tag", "").casefold() == self.provider
            ]
        if not endpoints:
            raise ValueError(
                f"No OpenRouter endpoint is available for {self.model}/{self.provider}."
            )
        if structured_output and not any(
            {"response_format", "structured_outputs"}.issubset(item.get("supported_parameters", []))
            for item in endpoints
        ):
            selected = self.provider or "available providers"
            raise ValueError(
                f"{self.model} through {selected} does not advertise strict structured output; "
                "choose a provider supporting response_format and structured_outputs."
            )


def api_configuration(model: str, api: str, provider: str | None, settings: dict) -> ModelConfig:
    """Resolve credentials without changing the process environment or revealing tokens."""
    environment = dict(os.environ)
    if api not in {"openai", "openrouter"}:
        raise ValueError(f"Unknown model API: {api}")
    if api == "openrouter":
        token = next(
            (
                source.get(name)
                for source in (environment, settings)
                for name in ("OPENROUTER_API_KEY", "OPENROUTER_TOKEN")
                if source.get(name)
            ),
            "",
        )
        if not token.strip():
            raise ValueError(
                "Set OPENROUTER_API_KEY or OPENROUTER_TOKEN in the environment or env file."
            )
        return OpenRouterConfig(model, token, provider)
    if provider is not None:
        raise ValueError("--review-provider requires --review-api openrouter.")
    token = environment.get("OPENAI_API_KEY", settings.get("OPENAI_API_KEY", ""))
    if not token or not token.strip():
        raise ValueError("Set OPENAI_API_KEY for native Agents SDK requests.")
    return OpenAIConfig(model, token)
