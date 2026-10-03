"""Search parameters and the same local filters used by OpenRent's website."""

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
    """Great-circle distance in kilometres; OpenRent's display distances are rounded."""
    a1, a2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = a2 - a1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, a)))


@dataclass
class SearchOptions:
    location: str
    radius_distance: float | None = None
    radius_minutes: int | None = None
    distance_unit: str = "km"
    rent_min: Decimal | None = None
    rent_max: Decimal | None = None
    bedrooms_min: int | None = None
    bedrooms_max: int | None = None
    bathrooms_min: int | None = None
    bathrooms_max: int | None = None
    property_types: tuple[str, ...] = ()
    furnishing: str = "any"
    pets: bool = False
    students: bool = False
    professionals: bool = False
    families: bool = False
    dss: bool = False
    bills_included: bool = False
    garden: bool = False
    parking: bool = False
    fireplace: bool = False
    video: bool = False
    no_shared: bool = False
    no_studios: bool = False
    include_unavailable: bool = False
    move_in_before: date | None = None
    max_minimum_tenancy: int | None = None

    def __post_init__(self):
        if not self.location.strip():
            raise SearchError("Location must not be empty.")
        if (self.radius_distance is None) == (self.radius_minutes is None):
            raise SearchError("Choose exactly one of --radius-distance or --radius-minutes.")
        if self.distance_unit not in ("km", "miles"):
            raise SearchError("Distance unit must be km or miles.")
        for name in ("radius_distance", "radius_minutes"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise SearchError(f"{name.replace('_', '-')} must be positive and finite.")
        for name in ("rent", "bedrooms", "bathrooms"):
            lower, upper = getattr(self, f"{name}_min"), getattr(self, f"{name}_max")
            for value in (lower, upper):
                if value is not None and (not math.isfinite(value) or value < 0):
                    raise SearchError(f"{name} bounds must be non-negative and finite.")
            if lower is not None and upper is not None and lower > upper:
                raise SearchError(f"Minimum {name} must not exceed maximum {name}.")
        if self.max_minimum_tenancy is not None and self.max_minimum_tenancy < 0:
            raise SearchError("Maximum minimum tenancy must be non-negative.")
        if any(t not in ("house", "flat", "room") for t in self.property_types):
            raise SearchError("Property types must be house, flat, or room.")
        if self.furnishing not in ("any", "furnished", "unfurnished"):
            raise SearchError("Unknown furnishing preference.")

    @property
    def radius_km(self) -> float | None:
        if self.radius_distance is None:
            return None
        return self.radius_distance * (1.609344 if self.distance_unit == "miles" else 1)

    def parameters(self) -> dict[str, str]:
        """Public website parameters, also stored in normalized search_filters rows."""
        radius = self.radius_km
        params = {
            "term": self.location.strip(),
            "searchType": "minutes" if self.radius_minutes is not None else "km",
            # OpenRent silently ignores fractional area values. Fetch a superset
            # and enforce the user's exact distance using coordinates below.
            "area": str(self.radius_minutes + 1 if radius is None else math.ceil(radius) + 1),
            "isLive": "false" if self.include_unavailable else "true",
        }
        values = {
            "prices_min": self.rent_min,
            "prices_max": self.rent_max,
            "bedrooms_min": self.bedrooms_min,
            "bedrooms_max": self.bedrooms_max,
            "bathrooms_min": self.bathrooms_min,
            "bathrooms_max": self.bathrooms_max,
            "minTenancy": self.max_minimum_tenancy,
        }
        for key, value in values.items():
            if value is not None:
                params[key] = str(value)
        flags = {
            "acceptPets": self.pets,
            "acceptStudents": self.students,
            "acceptNonStudents": self.professionals,
            "acceptFamilies": self.families,
            "rentCoveredByDSSorPreferred": self.dss,
            "includeBills": self.bills_included,
            "hasGarden": self.garden,
            "hasParking": self.parking,
            "hasFireplace": self.fireplace,
            "videoTour": self.video,
        }
        for key, enabled in flags.items():
            if enabled:
                params[key] = "true"
        if self.furnishing != "any":
            params["furnishedType"] = "1" if self.furnishing == "furnished" else "2"
        if self.move_in_before:
            params["availableBefore"] = self.move_in_before.isoformat()
        if self.property_types:
            codes = {"house": "1", "flat": "2", "room": "3"}
            if len(set(self.property_types)) == 1:
                params["propertyType"] = codes[self.property_types[0]]
        return params

    def identity_parameters(self) -> dict[str, str]:
        params = self.parameters()
        # Store the precise user radius, distinct from the rounded server area.
        if self.radius_distance is not None:
            params["requestedRadiusKm"] = str(self.radius_km)
        params["requestedPropertyTypes"] = ",".join(sorted(set(self.property_types)))
        params["excludeShared"] = str(self.no_shared).lower()
        params["excludeStudios"] = str(self.no_studios).lower()
        return params

    def matches(self, candidate: Candidate, search: SearchData) -> bool:
        """Evaluate requested filters, failing if the source cannot supply their facts."""
        prop = candidate.property

        def require(value, field, filter_name):
            if value is None:
                raise SearchError(
                    f"Property {prop.id} is missing {field}; cannot evaluate {filter_name}."
                )
            return value

        # Validate all requested facts before excluding a candidate. Otherwise a
        # missing optional source array could silently turn a scan into no matches.
        if not self.include_unavailable:
            require(prop.is_live, "is_live", "the live-only search")
        bed_count = None
        if self.bedrooms_min is not None or self.bedrooms_max is not None:
            require(prop.is_shared, "is_shared", "bedroom limits")
            if prop.is_shared:
                bed_count = -1
            else:
                require(prop.is_studio, "is_studio", "bedroom limits")
                bed_count = (
                    0 if prop.is_studio else require(prop.bedrooms, "bedrooms", "bedroom limits")
                )
        bounds = (
            (
                "rent_pcm_pence",
                "rent",
                prop.rent_pcm_pence,
                pence(self.rent_min) if self.rent_min is not None else None,
                pence(self.rent_max) if self.rent_max is not None else None,
            ),
            ("bedrooms", "bedrooms", bed_count, self.bedrooms_min, self.bedrooms_max),
            ("bathrooms", "bathrooms", prop.bathrooms, self.bathrooms_min, self.bathrooms_max),
        )
        for field, option, value, lower, upper in bounds:
            if lower is not None or upper is not None:
                require(value, field, f"--{option}-{'min' if lower is not None else 'max'}")
        if self.no_shared:
            require(prop.is_shared, "is_shared", "--no-shared")
        if self.no_studios:
            require(prop.is_studio, "is_studio", "--no-studios")
        if self.property_types:
            require(prop.property_type_code, "property_type_code", "--property-type")
        features = (
            (self.pets, "pets_allowed", "pets"),
            (self.students, "students_allowed", "students"),
            (self.professionals, "non_students_allowed", "professionals"),
            (self.families, "families_allowed", "families"),
            (self.dss, "dss_covers_rent", "dss"),
            (self.bills_included, "bills_included", "bills-included"),
            (self.garden, "garden", "garden"),
            (self.parking, "parking", "parking"),
            (self.fireplace, "fireplace", "fireplace"),
        )
        for requested, field, option in features:
            if requested:
                require(getattr(prop, field), field, f"--{option}")
        if self.video and prop.has_video is not True and prop.video_viewings is not True:
            require(prop.has_video, "has_video", "--video")
            require(prop.video_viewings, "video_viewings", "--video")
        if self.furnishing != "any":
            require(getattr(prop, self.furnishing), self.furnishing, "--furnishing")
        if self.max_minimum_tenancy is not None:
            require(prop.minimum_tenancy_months, "minimum_tenancy_months", "--max-minimum-tenancy")
        available_from = None
        if self.move_in_before:
            require(prop.available_from, "available_from", "--move-in-before")
            try:
                available_from = date.fromisoformat(prop.available_from)
            except ValueError as exc:
                raise SearchError(
                    f"Property {prop.id} has invalid available_from; cannot evaluate --move-in-before."
                ) from exc

        if not self.include_unavailable and prop.is_live is not True:
            return False
        if self.radius_minutes is not None:
            if candidate.commute_minutes is None:
                raise SearchError(
                    "OpenRent did not return commute times; refusing a distance fallback."
                )
            if candidate.commute_minutes > self.radius_minutes:
                return False
        else:
            if None not in (search.latitude, search.longitude, prop.latitude, prop.longitude):
                candidate.distance_km = distance_km(
                    search.latitude, search.longitude, prop.latitude, prop.longitude
                )
            if candidate.distance_km is None:
                raise SearchError(f"Property {prop.id} has no usable location or distance.")
            if candidate.distance_km > self.radius_km:
                return False
        for _, _, value, lower, upper in bounds:
            if lower is not None and value < lower:
                return False
            if upper is not None and value > upper:
                return False
        if self.no_shared and prop.is_shared:
            return False
        if self.no_studios and prop.is_studio:
            return False
        if self.property_types:
            codes = {"house": 1, "flat": 2, "room": 3}
            if prop.property_type_code not in {codes[t] for t in self.property_types}:
                return False
        for requested, field, _ in features:
            if requested and getattr(prop, field) is not True:
                return False
        if self.video and not (prop.has_video or prop.video_viewings):
            return False
        if self.furnishing != "any" and getattr(prop, self.furnishing) is not True:
            return False
        if (
            self.max_minimum_tenancy is not None
            and prop.minimum_tenancy_months > self.max_minimum_tenancy
        ):
            return False
        return self.move_in_before is None or available_from <= self.move_in_before
