"""Typed records shared by the parser, importer, and database layer.

Money is always stored as integer pence; missing booleans remain None.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Image:
    source_url: str
    position: int = 0
    kind: str = "photo"
    caption: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class Feature:
    key: str
    label: str
    section: str = ""
    value: str | int | float | bool | None = None
    unit: str | None = None


@dataclass
class NearbyPlace:
    name: str
    kind: str = "transport"
    walking_minutes: int | None = None
    distance_km: float | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class MediaLink:
    url: str
    kind: str = "video"
    caption: str | None = None


@dataclass
class Property:
    id: int
    url: str
    title: str | None = None
    description: str | None = None
    description_html: str | None = None
    property_type: str | None = None
    property_type_code: int | None = None
    address_display: str | None = None
    locality: str | None = None
    postcode: str | None = None
    country: str = "GB"
    latitude: float | None = None
    longitude: float | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    max_tenants: int | None = None
    rent_pcm_pence: int | None = None
    rent_weekly_pence: int | None = None
    deposit_pence: int | None = None
    currency: str = "GBP"
    available_from: str | None = None
    minimum_tenancy_months: int | None = None
    maximum_tenancy_months: int | None = None
    furnished: bool | None = None
    unfurnished: bool | None = None
    furnishing: str | None = None
    bills_included: bool | None = None
    pets_allowed: bool | None = None
    students_allowed: bool | None = None
    non_students_allowed: bool | None = None
    families_allowed: bool | None = None
    dss_covers_rent: bool | None = None
    garden: bool | None = None
    parking: bool | None = None
    fireplace: bool | None = None
    smokers_allowed: bool | None = None
    has_video: bool | None = None
    video_viewings: bool | None = None
    is_shared: bool | None = None
    is_studio: bool | None = None
    is_live: bool | None = None
    status: str | None = None
    first_listed_at: str | None = None
    epc_rating: str | None = None
    landlord_name: str | None = None
    landlord_member_since: str | None = None
    landlord_last_active: str | None = None
    source_html: str | None = None
    detail_complete: bool = False
    # Only irregular nested data from source schemas belongs here. Known facts
    # have explicit columns and scalar feature rows instead.
    extra_metadata: dict[str, Any] = field(default_factory=dict)
    features: list[Feature] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    nearby_places: list[NearbyPlace] = field(default_factory=list)
    media_links: list[MediaLink] = field(default_factory=list)


@dataclass
class Candidate:
    property: Property
    distance_km: float | None = None
    commute_minutes: float | None = None


@dataclass
class SearchData:
    candidates: list[Candidate]
    latitude: float | None = None
    longitude: float | None = None
    location: str | None = None
    distance_unit: str | None = None
    total: int | None = None
