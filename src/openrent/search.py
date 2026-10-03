"""Two filter models: geographic API parameters and locally evaluated website choices."""

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import Candidate, SearchData


class SearchError(ValueError):
    """The requested search cannot be evaluated reliably."""


def pence(value: Decimal | str | float) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal(1)))


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a1, a2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = a2 - a1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, a)))


@dataclass
class ApiFilters:
    location: str | None = None
    radius_distance: float | None = None
    radius_minutes: int | None = None
    distance_unit: str | None = None

    def __post_init__(self):
        if self.location is not None and not self.location.strip():
            raise SearchError("Location must not be empty.")
        if self.radius_distance is not None and self.radius_minutes is not None:
            raise SearchError("Choose exactly one of --radius-distance or --radius-minutes.")
        if self.distance_unit not in (None, "km", "miles"):
            raise SearchError("Distance unit must be km or miles.")
        for value in (self.radius_distance, self.radius_minutes):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise SearchError("Radius must be positive and finite.")

    @property
    def radius_km(self) -> float | None:
        if self.radius_distance is None:
            return None
        return self.radius_distance * (1.609344 if self.distance_unit == "miles" else 1)

    def parameters(self) -> dict[str, str]:
        params = {"term": self.location.strip()} if self.location is not None else {}
        radius = self.radius_minutes if self.radius_minutes is not None else self.radius_km
        if radius is not None:
            # Integer area only; fetch a superset and enforce exact limits locally.
            params.update(
                searchType="minutes" if self.radius_minutes is not None else "km",
                area=str(math.ceil(radius) + 1),
            )
        return params


@dataclass
class WebsiteFilters:
    """None means no constraint. Feature flags require a feature only when True."""

    rent_min: Decimal | None = None
    rent_max: Decimal | None = None
    bedrooms_min: int | None = None
    bedrooms_max: int | None = None
    bathrooms_min: int | None = None
    bathrooms_max: int | None = None
    property_types: tuple[str, ...] | None = None
    furnishing: str | None = None
    pets: bool | None = None
    students: bool | None = None
    professionals: bool | None = None
    families: bool | None = None
    dss: bool | None = None
    bills_included: bool | None = None
    garden: bool | None = None
    parking: bool | None = None
    fireplace: bool | None = None
    video: bool | None = None
    no_shared: bool | None = None
    no_studios: bool | None = None
    include_unavailable: bool | None = None
    move_in_before: date | None = None
    max_minimum_tenancy: int | None = None

    def __post_init__(self):
        for name in ("rent", "bedrooms", "bathrooms"):
            lower, upper = getattr(self, f"{name}_min"), getattr(self, f"{name}_max")
            if any(v is not None and (not math.isfinite(v) or v < 0) for v in (lower, upper)):
                raise SearchError(f"{name} bounds must be non-negative and finite.")
            if lower is not None and upper is not None and lower > upper:
                raise SearchError(f"Minimum {name} must not exceed maximum {name}.")
        if self.max_minimum_tenancy is not None and self.max_minimum_tenancy < 0:
            raise SearchError("Maximum minimum tenancy must be non-negative.")
        if any(t not in ("house", "flat", "room") for t in self.property_types or ()):
            raise SearchError("Property types must be house, flat, or room.")
        if self.furnishing not in (None, "any", "furnished", "unfurnished"):
            raise SearchError("Unknown furnishing preference.")


def matches_criteria(
    api: ApiFilters, website: WebsiteFilters, candidate: Candidate, search: SearchData
) -> bool:
    """Check all requested facts before rejecting, so missing source data stays visible."""
    prop, checks = candidate.property, []

    def require(value, field, option):
        if value is None:
            raise SearchError(f"Property {prop.id} is missing {field}; cannot evaluate {option}.")
        return value

    def feature(field, expected, option):
        checks.append(require(getattr(prop, field), field, option) is expected)

    if website.include_unavailable is False:
        feature("is_live", True, "the live-only search")
    for name in ("rent", "bedrooms", "bathrooms"):
        lower, upper = getattr(website, f"{name}_min"), getattr(website, f"{name}_max")
        if lower is None and upper is None:
            continue
        field = "rent_pcm_pence" if name == "rent" else name
        value = getattr(prop, field)
        if name == "bedrooms":
            if require(prop.is_shared, "is_shared", "bedroom limits"):
                value = -1
            elif require(prop.is_studio, "is_studio", "bedroom limits"):
                value = 0
        value = require(value, field, f"--{name}-{'min' if lower is not None else 'max'}")
        if name == "rent":
            lower, upper = (pence(v) if v is not None else None for v in (lower, upper))
        checks.append((lower is None or value >= lower) and (upper is None or value <= upper))
    for option, field in (
        ("pets", "pets_allowed"),
        ("students", "students_allowed"),
        ("professionals", "non_students_allowed"),
        ("families", "families_allowed"),
        ("dss", "dss_covers_rent"),
        ("bills_included", "bills_included"),
        ("garden", "garden"),
        ("parking", "parking"),
        ("fireplace", "fireplace"),
        ("no_shared", "is_shared"),
        ("no_studios", "is_studio"),
    ):
        if getattr(website, option):
            feature(field, not option.startswith("no_"), "--" + option.replace("_", "-"))
    if website.property_types:
        code = require(prop.property_type_code, "property_type_code", "--property-type")
        checks.append(
            code in {{"house": 1, "flat": 2, "room": 3}[t] for t in website.property_types}
        )
    if website.furnishing not in (None, "any"):
        feature(website.furnishing, True, "--furnishing")
    if website.video and prop.has_video is not True and prop.video_viewings is not True:
        require(prop.has_video, "has_video", "--video")
        require(prop.video_viewings, "video_viewings", "--video")
        checks.append(False)
    if website.max_minimum_tenancy is not None:
        value = require(
            prop.minimum_tenancy_months, "minimum_tenancy_months", "--max-minimum-tenancy"
        )
        checks.append(value <= website.max_minimum_tenancy)
    if website.move_in_before is not None:
        value = require(prop.available_from, "available_from", "--move-in-before")
        try:
            checks.append(date.fromisoformat(value) <= website.move_in_before)
        except ValueError as exc:
            raise SearchError(
                f"Property {prop.id} has invalid available_from; cannot evaluate --move-in-before."
            ) from exc
    if api.radius_minutes is not None:
        if candidate.commute_minutes is None:
            raise SearchError(
                "OpenRent did not return commute times; refusing a distance fallback."
            )
        checks.append(candidate.commute_minutes <= api.radius_minutes)
    elif api.radius_km is not None:
        if None not in (search.latitude, search.longitude, prop.latitude, prop.longitude):
            candidate.distance_km = distance_km(
                search.latitude, search.longitude, prop.latitude, prop.longitude
            )
        if candidate.distance_km is None:
            raise SearchError(f"Property {prop.id} has no usable location or distance.")
        checks.append(candidate.distance_km <= api.radius_km)
    return all(checks)
