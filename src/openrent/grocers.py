"""OpenStreetMap grocery shops and pedestrian travel times from Valhalla."""

from __future__ import annotations

import math
from builtins import type as ModelType
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from .amenity_client import WALKING_SPEED_KMH, AmenityClient
from .client import FetchError
from .models import Property

_GROCER_SHOPS = {"supermarket", "convenience", "greengrocer", "grocery", "food"}


class Grocer(BaseModel):
    osm_id: str
    name: str
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    classification: str
    tags: dict[str, str]
    walking_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    walking_distance_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class GrocerAmenities(BaseModel):
    grocers: list[Grocer]
    origin_latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    origin_longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    search_radius_m: int = Field(gt=0)
    walking_speed_kmh: float = Field(gt=0, allow_inf_nan=False)


class GrocerMetadataFetcher:
    type: ClassVar[str] = "grocer"
    model: ClassVar[ModelType[BaseModel]] = GrocerAmenities

    def __init__(self, client: AmenityClient | None, *, radius_m: int = 1500):
        if client is None:
            raise RuntimeError("Amenity HTTP client has not been configured")
        self.client = client
        self.radius_m = radius_m

    async def fetch(self, property: Property) -> dict:
        latitude, longitude = property.latitude, property.longitude
        if (
            latitude is None
            or longitude is None
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise FetchError(f"Property {property.id} needs coordinates for the grocer lookup")
        query = (
            "[out:json][timeout:30];("
            + "".join(
                f'nwr(around:{self.radius_m},{latitude},{longitude})["shop"="{shop}"];'
                for shop in sorted(_GROCER_SHOPS)
            )
            + ");out center tags;"
        )
        elements = await self.client.osm_elements(query)
        try:
            grocers = {}
            for element in elements:
                shop = element.tags.get("shop")
                if shop not in _GROCER_SHOPS or element.tags.get("access") in {"private", "no"}:
                    continue
                coordinates = element.center or {"lat": element.lat, "lon": element.lon}
                grocer = Grocer(
                    osm_id=f"{element.type}/{element.id}",
                    name=element.tags.get("name")
                    or element.tags.get("brand")
                    or element.tags.get("operator")
                    or "Unnamed grocer",
                    latitude=coordinates.get("lat"),
                    longitude=coordinates.get("lon"),
                    classification=f"shop={shop}",
                    tags=element.tags,
                )
                grocers[grocer.osm_id] = grocer
            ordered = sorted(grocers.values(), key=lambda grocer: grocer.osm_id)
            routes = await self.client.walking_routes(
                latitude, longitude, [(grocer.latitude, grocer.longitude) for grocer in ordered]
            )
            for grocer, route in zip(ordered, routes, strict=True):
                grocer.walking_seconds = route.time
                grocer.walking_distance_m = (
                    route.distance * 1000 if route.distance is not None else None
                )
            return GrocerAmenities(
                grocers=ordered,
                origin_latitude=latitude,
                origin_longitude=longitude,
                search_radius_m=self.radius_m,
                walking_speed_kmh=WALKING_SPEED_KMH,
            ).model_dump(mode="json")
        except ValidationError as exc:
            raise FetchError(f"Grocer service returned invalid metadata: {exc}") from exc
