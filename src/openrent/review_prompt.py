"""Render archived rental facts as a readable dossier, without HTML or SQL bookkeeping."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from .backends import ImageEvidence

_BOOKKEEPING = {
    "source_html",
    "description_html",
    "first_seen_at",
    "last_seen_at",
    "updated_at",
    "property_type_code",
    "furnished",
    "unfurnished",
    "extra_metadata_json",
}
_TRANSPORT_KINDS = {
    "underground": "Underground",
    "national_rail": "National Rail",
    "transport": "Transport",
    "overground": "Overground",
    "dlr": "DLR",
    "tram": "Tram",
    "bus": "Bus",
}
_IMAGE_KINDS = {"photo": "Photograph", "floorplan": "Floorplan", "map": "Map"}


def _label(key: str) -> str:
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return key.replace("_", " ").replace("-", " ").capitalize()


def _value(value: Any, unit: str | None = None) -> str:
    if value is None or value == "":
        return "Not supplied"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if unit == "pence":
        pounds = Decimal(str(value)) / 100
        return f"£{pounds:,.0f}" if pounds == pounds.to_integral_value() else f"£{pounds:,.2f}"
    return str(value) + (f" {unit}" if unit else "")


def _boolean(value: Any) -> str:
    return "Not supplied" if value is None else "Yes" if value else "No"


def _nested(label: str, value: Any, indent: str = "") -> list[str]:
    """Preserve unfamiliar structured source facts as labelled text, not JSON."""
    if isinstance(value, Mapping):
        lines = [f"{indent}{label}:"]
        for key, item in value.items():
            lines.extend(_nested(_label(str(key)), item, indent + "  "))
        return lines if value else [f"{indent}{label}: None supplied"]
    if isinstance(value, (list, tuple)):
        lines = [f"{indent}{label}:"]
        for number, item in enumerate(value, 1):
            lines.extend(_nested(f"Item {number}", item, indent + "  "))
        return lines if value else [f"{indent}{label}: None supplied"]
    return [f"{indent}{label}: {_value(value)}"]


def _place(place: Mapping[str, Any]) -> str:
    name = str(place.get("name") or "Unnamed place")
    kind = str(place.get("kind") or "transport")
    details = []
    if place.get("walking_minutes") is not None:
        details.append(f"{place['walking_minutes']} minutes' walk")
    else:
        details.append("walking time not supplied")
    if place.get("distance_km") is not None:
        details.append(f"{place['distance_km']} km")
    if place.get("latitude") is not None and place.get("longitude") is not None:
        details.append(f"coordinates {place['latitude']}, {place['longitude']}")
    return f"{name} ({_TRANSPORT_KINDS.get(kind, _label(kind))}): " + "; ".join(details)


def render_listing(snapshot: Mapping[str, Any], images: Sequence[ImageEvidence]) -> str:
    """Format version one's property evidence; original image bytes stay separate.

    Feature values are displayed using their existing parsed type and unit. New
    labels and extra source fields are retained automatically, without guessing
    their meaning or extracting facts from the marketing description.
    """
    consumed = set(_BOOKKEEPING)

    def fact(label: str, key: str, unit: str | None = None, *, boolean: bool = False) -> str:
        consumed.add(key)
        value = snapshot.get(key)
        return f"{label}: {_boolean(value) if boolean else _value(value, unit)}"

    address_keys = ("address_display", "locality", "postcode", "country")
    consumed.update(address_keys)
    address_parts = [str(snapshot[key]) for key in address_keys if snapshot.get(key)]
    if address_parts and address_parts[-1] == "GB":
        address_parts[-1] = "United Kingdom"
    consumed.update({"title", "latitude", "longitude", "description"})
    lines = [
        "Property:",
        _value(snapshot.get("title")),
        fact("OpenRent ID", "id"),
        fact("Listing", "url"),
        "Address: " + (", ".join(address_parts) if address_parts else "Not supplied"),
        (
            f"Location: Latitude {_value(snapshot.get('latitude'))}, "
            f"longitude {_value(snapshot.get('longitude'))}"
        ),
        "",
        fact("Rent per month", "rent_pcm_pence", "pence"),
        fact("Weekly rent displayed by OpenRent", "rent_weekly_pence", "pence"),
        fact("Deposit", "deposit_pence", "pence"),
        fact("Currency", "currency"),
        fact("Bedrooms", "bedrooms"),
        fact("Bathrooms", "bathrooms"),
        fact("Maximum tenants", "max_tenants"),
        fact("Property type", "property_type"),
        fact("Shared property", "is_shared", boolean=True),
        fact("Studio", "is_studio", boolean=True),
        fact("Furnishing", "furnishing"),
        fact("Bills included (listing fields)", "bills_included", boolean=True),
        fact("Available from", "available_from"),
        fact("Minimum tenancy", "minimum_tenancy_months", "months"),
        fact("Maximum tenancy", "maximum_tenancy_months", "months"),
        "",
        "Description (as written in the listing):",
        _value(snapshot.get("description")),
        "",
        "Features and restrictions (listing fields):",
    ]
    for label, key in (
        ("Garden", "garden"),
        ("Parking", "parking"),
        ("Fireplace", "fireplace"),
        ("Pets allowed", "pets_allowed"),
        ("Students allowed", "students_allowed"),
        ("Non-students allowed", "non_students_allowed"),
        ("Families allowed", "families_allowed"),
        ("Smokers allowed", "smokers_allowed"),
        ("DSS/LHA covers the rent", "dss_covers_rent"),
        ("Online/video viewings", "video_viewings"),
        ("Has video", "has_video"),
    ):
        lines.append(fact(label, key, boolean=True))
    lines.append(fact("EPC rating", "epc_rating"))

    consumed.add("features")
    for feature in snapshot.get("features") or ():
        key = str(feature.get("feature_key") or "")
        # These are site viewer controls or contact verification, rather than
        # property facts in the approved prompt example.
        if key.startswith("streetview_") or key == "email_address_verified":
            continue
        value_type = feature.get("value_type")
        value = feature.get(f"value_{value_type}") if value_type else feature.get("value")
        if value_type == "boolean" and value is not None:
            value = bool(value)
        label = str(feature.get("label") or _label(key))
        section = feature.get("section")
        prefix = f"{section} — " if section else ""
        lines.append(f"{prefix}{label}: {_value(value, feature.get('unit'))}")

    consumed.add("nearby_places")
    places = snapshot.get("nearby_places") or ()
    lines.extend(
        [
            "",
            "Nearby transport:",
            "Distances and walking times are OpenRent's displayed figures, not independently verified.",
        ]
    )
    transport = [place for place in places if place.get("kind") != "school"]
    lines.extend(_place(place) for place in transport)
    if not transport:
        lines.append("Not supplied")

    lines.extend(["", "Additional information:"])
    for label, key in (
        ("Listing status", "status"),
        ("First listed", "first_listed_at"),
        ("Landlord", "landlord_name"),
        ("Landlord member since", "landlord_member_since"),
        ("Landlord last active", "landlord_last_active"),
    ):
        lines.append(fact(label, key))
    lines.append(fact("Live listing", "is_live", boolean=True))

    schools = [place for place in places if place.get("kind") == "school"]
    if schools:
        lines.extend(["", "Nearby schools (listing figures):"])
        lines.extend(_place(place) for place in schools)

    consumed.add("media_links")
    media = snapshot.get("media_links") or ()
    if media:
        lines.extend(["", "Property videos and other media:"])
        for link in media:
            label = str(link.get("caption") or _label(str(link.get("kind") or "Media")))
            lines.append(f"{label}: {_value(link.get('url'))}")

    consumed.add("search")
    search = snapshot.get("search")
    if search:
        lines.extend(
            [
                "",
                "Search context:",
                f"Search centred on: {_value(search.get('location'))}",
                (
                    f"Search centre coordinates: {_value(search.get('latitude'))}, "
                    f"{_value(search.get('longitude'))}"
                ),
                f"Search-reported distance from the centre: {_value(search.get('distance_km'), 'km')}",
                f"Search-reported commute time: {_value(search.get('commute_minutes'), 'minutes')}",
            ]
        )
        for item in search.get("filters") or ():
            lines.append(f"Search filter {_label(str(item['name']))}: {_value(item.get('value'))}")

    consumed.add("extra_metadata")
    extras = snapshot.get("extra_metadata")
    if extras:
        lines.extend(["", "Other source facts:"])
        lines.extend(_nested("Additional listing data", extras))
    for key, value in snapshot.items():
        if key not in consumed and key != "images":
            lines.extend(_nested(_label(key), value))

    lines.extend(["", "Images:", f"{len(images)} images supplied in the listing's gallery order."])
    gallery_metadata = snapshot.get("images") or ()
    for number, image in enumerate(images, 1):
        metadata = gallery_metadata[number - 1] if number <= len(gallery_metadata) else {}
        kind = getattr(image, "kind", None) or metadata.get("kind")
        caption = getattr(image, "caption", None) or metadata.get("caption")
        label = _IMAGE_KINDS.get(kind, _label(kind) if kind else "Image")
        line = f"Image {number}: {label}"
        if caption:
            line += f" — {caption}"
        lines.append(line)
    return "\n".join(lines) + "\n"
