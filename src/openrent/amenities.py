"""Contracts for independent amenity metadata and SQL-backed property filters."""

from builtins import type as ModelType
from typing import Any, ClassVar, Protocol

import aiosqlite
from pydantic import BaseModel

from . import db as database
from .models import Property


class MetadataFetcher(Protocol):
    type: ClassVar[str]
    model: ClassVar[ModelType[BaseModel]]

    async def fetch(self, property: Property) -> dict[str, Any]: ...


async def fetch_and_store_amenities(
    db: aiosqlite.Connection, property: Property, fetcher: MetadataFetcher
) -> None:
    """Fetch a missing type once, validate it, and save its JSONB payload."""
    if fetcher.type in await database.get_amenities(db, property.id):
        return
    payload = fetcher.model.model_validate(await fetcher.fetch(property))
    await database.store_amenity(db, property.id, fetcher.type, payload.model_dump_json())
