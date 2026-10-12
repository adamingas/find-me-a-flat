"""OpenStreetMap gym metadata and pedestrian travel times from Valhalla."""

from __future__ import annotations

import math
import re
from builtins import type as ModelType
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from .amenity_client import WALKING_SPEED_KMH, AmenityClient
from .client import FetchError
from .models import Property

_FITNESS_SPORTS = {"fitness", "bodybuilding", "weightlifting", "crossfit"}
_CLASS_ONLY_SPORTS = {
    "yoga",
    "pilates",
    "dance",
    "martial_arts",
    "jiu_jitsu",
    "judo",
    "karate",
    "aikido",
}


class Gym(BaseModel):
    osm_id: str
    name: str
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    classification: str
    tags: dict[str, str]
    walking_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    walking_distance_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class GymAmenities(BaseModel):
    gyms: list[Gym]
    origin_latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    origin_longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    search_radius_m: int = Field(gt=0)
    walking_speed_kmh: float = Field(gt=0, allow_inf_nan=False)


def _classification(tags: dict[str, str]) -> str | None:
    """Include multi-sport leisure centres, but exclude single-activity studios/courts."""
    if tags.get("access") in {"private", "no"} or tags.get("fitness_centre") == "no":
        return None
    sports = {sport.strip().lower() for sport in tags.get("sport", "").split(";")} - {""}
    if sports and sports <= _CLASS_ONLY_SPORTS:
        return None
    if tags.get("amenity") == "gym":
        return "amenity=gym"
    if tags.get("leisure") == "fitness_centre":
        return "leisure=fitness_centre"
    if tags.get("leisure") == "sports_centre":
        if tags.get("fitness_centre") == "yes" or sports & _FITNESS_SPORTS:
            return "sports_centre with fitness facilities"
        if "multi" in sports:
            return "multi-sport centre"
        name = tags.get("name", "").casefold()
        if not sports and re.search(r"\bgym\b|\bfitness\b|\bleisure cent(?:re|er)\b", name):
            return "sports_centre named as a gym or leisure centre"
    return None


class GymMetadataFetcher:
    type: ClassVar[str] = "gym"
    model: ClassVar[ModelType[BaseModel]] = GymAmenities

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
            raise FetchError(f"Property {property.id} needs coordinates for the gym lookup")
        query = (
            "[out:json][timeout:30];("
            f'nwr(around:{self.radius_m},{latitude},{longitude})["leisure"="fitness_centre"];'
            f'nwr(around:{self.radius_m},{latitude},{longitude})["amenity"="gym"];'
            f'nwr(around:{self.radius_m},{latitude},{longitude})["leisure"="sports_centre"];'
            ");out center tags;"
        )
        elements = await self.client.osm_elements(query)
        try:
            gyms = {}
            for element in elements:
                classification = _classification(element.tags)
                if classification is None:
                    continue
                coordinates = element.center or {"lat": element.lat, "lon": element.lon}
                gym = Gym(
                    osm_id=f"{element.type}/{element.id}",
                    name=element.tags.get("name") or element.tags.get("operator") or "Unnamed gym",
                    latitude=coordinates.get("lat"),
                    longitude=coordinates.get("lon"),
                    classification=classification,
                    tags=element.tags,
                )
                gyms[gym.osm_id] = gym
            ordered = sorted(gyms.values(), key=lambda gym: gym.osm_id)
            routes = await self.client.walking_routes(
                latitude, longitude, [(gym.latitude, gym.longitude) for gym in ordered]
            )
            for gym, route in zip(ordered, routes, strict=True):
                gym.walking_seconds = route.time
                gym.walking_distance_m = (
                    route.distance * 1000 if route.distance is not None else None
                )
            return GymAmenities(
                gyms=ordered,
                origin_latitude=latitude,
                origin_longitude=longitude,
                search_radius_m=self.radius_m,
                walking_speed_kmh=WALKING_SPEED_KMH,
            ).model_dump(mode="json")
        except ValidationError as exc:
            raise FetchError(f"Gym service returned invalid metadata: {exc}") from exc
