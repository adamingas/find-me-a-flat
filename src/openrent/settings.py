"""Credentials loaded once per daemon process, never saved in workflow inputs."""

from functools import cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    resend_token: str = Field(default="", repr=False)
    openai_api_key: str = Field(default="", repr=False)
    openrouter_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "OPENROUTER_TOKEN"),
        repr=False,
    )


@cache
def load_settings(env_file: Path) -> Settings:
    return Settings(_env_file=env_file)
