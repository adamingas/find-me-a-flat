"""Small HTML and plain-text rental digests, rendered entirely from archived facts."""

from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

# TfL's Victoria station hub: https://api.tfl.gov.uk/StopPoint/940GZZLUVIC
VICTORIA_STATION_COORDINATES = (51.495812, -0.143826)


@dataclass(frozen=True)
class DigestListing:
    property_id: int
    title: str
    url: str
    property_data: Mapping[str, Any]
    review_result: Mapping[str, Any]


@dataclass(frozen=True)
class EmailDigest:
    subject: str
    html: str
    text: str
    property_ids: tuple[int, ...]


_CRITERIA = (
    ("no_living_room_carpet", "No living-room carpet", True),
    ("bathroom_without_window", "Bathroom has no window", False),
    ("kitchen_counter_space_for_four_appliances", "Counter space for four appliances", False),
    ("gas_hob_or_induction_stovetop", "Gas hob or induction stovetop", False),
    ("bedroom_carpet", "Bedroom has carpet", False),
    ("area_at_least_50_m2", "At least 50 m²", True),
    ("primary_bedroom_fits_super_king_bed", "Super king bed fits (1.8 × 2 m)", True),
    ("not_ground_floor", "Not ground floor", False),
)
_CELL_STYLE = "padding:7px 9px;border:1px solid #ddd;text-align:left;vertical-align:top"


def _compact(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        return "Unknown"
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(" ", 1)[0]
    return (shortened or text[: limit - 1]) + "…"


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "Unknown"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _distance(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return f"{_number(value)} km"


def _walk(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return f"{_number(value)} min walk"


def _place_metrics(place: Mapping[str, Any]) -> str:
    values = (_distance(place.get("distance_km")), _walk(place.get("walking_minutes")))
    return "; ".join(value for value in values if value) or "distance/walk unknown"


def _nearest_tube(places: Sequence[Mapping[str, Any]]) -> str:
    tube = [place for place in places if place.get("kind") == "underground"]
    if not tube:
        return "Unknown"
    # The archive often supplies only walking times. Select the shortest reported
    # walk when present; use recorded distances when no walking times are supplied.
    walks = [place for place in tube if _walk(place.get("walking_minutes")) is not None]
    distances = [place for place in tube if _distance(place.get("distance_km")) is not None]
    if walks:
        nearest = min(walks, key=lambda place: place["walking_minutes"])
    elif distances:
        nearest = min(distances, key=lambda place: place["distance_km"])
    else:
        return "Unknown (no distances or walking times supplied)"
    return f"{_compact(nearest.get('name'), 100)} — {_place_metrics(nearest)}"


def _straight_line_km(
    latitude: Any, longitude: Any, target: tuple[float, float] | None
) -> float | None:
    if target is None or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in (latitude, longitude)
    ):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (latitude, longitude, *target))
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, haversine)))


def _victoria_distance(data: Mapping[str, Any], coordinates: tuple[float, float] | None) -> str:
    for place in data.get("nearby_places") or ():
        name = " ".join(str(place.get("name") or "").lower().split())
        if name not in {"victoria", "victoria station", "london victoria"}:
            continue
        if place.get("kind") not in {"underground", "national_rail", "transport"}:
            continue
        if _distance(place.get("distance_km")) or _walk(place.get("walking_minutes")):
            return _place_metrics(place) + " (listing)"
    distance = _straight_line_km(data.get("latitude"), data.get("longitude"), coordinates)
    return f"{_number(distance)} km (straight-line)" if distance is not None else "Unknown"


def _rent(data: Mapping[str, Any]) -> str:
    amount = data.get("rent_pcm_pence")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return "Unknown"
    pounds = Decimal(amount) / 100
    value = f"{pounds:,.0f}" if pounds == pounds.to_integral_value() else f"{pounds:,.2f}"
    currency = data.get("currency") or "GBP"
    return (f"£{value}" if currency == "GBP" else f"{currency} {value}") + " / month"


def _numerical_result(value: Any, unit: str = "") -> tuple[str, str]:
    if not isinstance(value, Mapping) or value.get("value") is None:
        return "Unknown", "No defensible value available."
    number = _number(value.get("value"))
    if unit and number != "Unknown":
        number += " " + unit
    basis = (
        f"Estimated ({_compact(value.get('certainty'), 20).lower()})"
        if "certainty" in value
        else "Stated"
    )
    return f"{number} · {basis}", _compact(value.get("evidence"))


def _review_rows(result: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    rows = []
    for name, label, breaking in _CRITERIA:
        criterion = result.get(name)
        if not isinstance(criterion, Mapping):
            criterion = {}
        label += " (required)" if breaking else ""
        rows.append(
            (
                label,
                _boolean_result(criterion.get("outcome")),
                _compact(criterion.get("evidence")),
            )
        )
    rows.append(("Area", *_numerical_result(result.get("area_m2"), "m²")))
    rows.append(("Floor (ground = 0)", *_numerical_result(result.get("floor"))))
    return rows


def _boolean_result(value: Any) -> str:
    """Render strict booleans and preserve older archived outcomes without migrating them."""
    if value is True or value == "met":
        return "True"
    if value is False or value == "not_met":
        return "False"
    return "Unknown"


def _safe_link(url: Any) -> str | None:
    if not isinstance(url, str) or any(ord(character) < 32 for character in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return url if parsed.scheme.lower() in {"https", "http"} and parsed.netloc else None


def _gallery_images(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Use every distinct safe archived image URL in stable source-gallery order."""
    images = [image for image in data.get("images") or () if isinstance(image, Mapping)]

    def order(item: tuple[int, Mapping[str, Any]]) -> tuple[float, int]:
        index, image = item
        position = image.get("position")
        valid = (
            not isinstance(position, bool)
            and isinstance(position, (int, float))
            and math.isfinite(position)
            and position >= 0
        )
        return (position if valid else index, index)

    gallery = []
    seen = set()
    for _, image in sorted(enumerate(images), key=order):
        url = _safe_link(image.get("source_url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        gallery.append({**image, "source_url": url})
    return gallery


def _gallery(listing: DigestListing, url: str | None) -> tuple[str, str]:
    """A native scrolling carousel with a visible listing link as the email-client fallback.

    No script, radio input or hidden initial image is needed. Fragment controls
    work in browser previews; email clients may strip them or scrolling styles.
    """
    images = _gallery_images(listing.property_data)
    listing_link = (
        f'<a href="{html.escape(url, quote=True)}" style="color:#1d4ed8">'
        f"View all listing images on OpenRent</a>"
        if url
        else ""
    )
    if not images:
        return (
            '<p style="font-size:13px;color:#666">No archived images available.'
            + (" " + listing_link if listing_link else "")
            + "</p>",
            "Gallery: No archived images available.",
        )
    count = len(images)
    prefix = f"flat-{listing.property_id}-image-"
    slides = []
    for index, image in enumerate(images, 1):
        kind = {"photo": "Photo", "floorplan": "Floor plan", "map": "Map"}.get(
            str(image.get("kind")), "Image"
        )
        caption = _compact(image.get("caption") or f"{kind} {index}", 150)
        escaped_caption = html.escape(caption, quote=True)
        picture = (
            f'<img src="{html.escape(image["source_url"], quote=True)}" '
            f'alt="{escaped_caption}" width="740" height="360" '
            'style="display:block;width:100%;max-width:740px;height:360px;'
            'object-fit:contain;border:0;background:#f3f4f6">'
        )
        if url:
            picture = f'<a href="{html.escape(url, quote=True)}">' + picture + "</a>"
        previous = index - 1 if index > 1 else count
        following = index + 1 if index < count else 1
        controls = (
            f'<a href="#{prefix}{previous}" style="color:#1d4ed8">‹ Previous</a> · '
            f"{index}/{count} · {escaped_caption} · "
            f'<a href="#{prefix}{following}" style="color:#1d4ed8">Next ›</a>'
            if count > 1
            else f"1/1 · {escaped_caption}"
        )
        slides.append(
            f'<figure id="{prefix}{index}" class="flat-gallery-slide" '
            'style="display:inline-block;vertical-align:top;width:100%;max-width:740px;'
            'margin:0;white-space:normal;scroll-snap-align:start">'
            + picture
            + '<figcaption style="padding:8px;font-size:12px;text-align:center">'
            + controls
            + "</figcaption></figure>"
        )
    gallery = (
        '<div class="flat-gallery" role="group" aria-label="Listing image gallery" '
        'style="margin:12px 0 18px">'
        '<div class="flat-gallery-track" style="width:100%;max-width:740px;overflow-x:auto;'
        "overflow-y:hidden;white-space:nowrap;scroll-snap-type:x mandatory;"
        'scroll-behavior:smooth;border:1px solid #ddd">'
        + "".join(slides)
        + "</div>"
        + '<p style="margin:8px 0;font-size:12px;color:#666">'
        + f"{count} archived images. "
        + (listing_link + ". " if listing_link else "")
        + "Swipe or use Previous/Next where supported.</p></div>"
    )
    return gallery, f"Gallery: {count} archived images; view the full gallery on the listing."


def _table(rows: Sequence[Sequence[str]], *, headings: Sequence[str] = ()) -> str:
    html_rows = []
    if headings:
        html_rows.append(
            "<tr>"
            + "".join(
                f'<th style="{_CELL_STYLE};background:#f3f4f6">{html.escape(heading)}</th>'
                for heading in headings
            )
            + "</tr>"
        )
    for row in rows:
        html_rows.append(
            "<tr>"
            + "".join(f'<td style="{_CELL_STYLE}">{html.escape(value)}</td>' for value in row)
            + "</tr>"
        )
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:13px">'
        + "".join(html_rows)
        + "</table>"
    )


def render_digest(
    listings: Sequence[DigestListing],
    *,
    victoria_coordinates: tuple[float, float] | None = VICTORIA_STATION_COORDINATES,
) -> EmailDigest:
    """Render a supplied accepted batch; delivery selection and deduplication happen elsewhere."""
    if not listings:
        raise ValueError("an email digest requires at least one listing")
    property_ids = tuple(listing.property_id for listing in listings)
    if len(set(property_ids)) != len(property_ids):
        raise ValueError("an email digest must not contain duplicate property IDs")
    subject = f"{len(listings)} new flats found"
    sections = []
    text_sections = []
    for number, listing in enumerate(listings, 1):
        data, result = listing.property_data, listing.review_result
        title = _compact(listing.title or f"OpenRent property {listing.property_id}", 180)
        url = _safe_link(listing.url)
        linked_title = (
            f'<a href="{html.escape(url, quote=True)}" style="color:#1d4ed8">'
            f"{html.escape(title)}</a>"
            if url
            else html.escape(title)
        )
        facts = [
            ("Rent", _rent(data)),
            ("Bedrooms", _number(data.get("bedrooms"))),
            ("Postcode", _compact(data.get("postcode"), 30)),
            ("Nearest reported Tube", _nearest_tube(data.get("nearby_places") or ())),
            ("Distance to Victoria", _victoria_distance(data, victoria_coordinates)),
        ]
        summary = _compact(result.get("summary"), 450)
        decision = {"pass": "Accepted", "reject": "Rejected", "uncertain": "Uncertain"}.get(
            str(result.get("decision")), "Unknown"
        )
        rows = _review_rows(result)
        gallery, gallery_text = _gallery(listing, url)
        sections.append(
            f'<h2 style="font-size:19px;margin:24px 0 12px">{number}. {linked_title}</h2>'
            + gallery
            + _table(facts)
            + f'<p style="margin:12px 0"><strong>{html.escape(decision)}.</strong> '
            + html.escape(summary)
            + "</p>"
            + _table(rows, headings=("Criterion", "Result", "Evidence"))
        )
        text_sections.append(
            f"{number}. {title}"
            + (f"\n{url}" if url else "")
            + f"\n{gallery_text}"
            + "\n"
            + "\n".join(f"{label}: {value}" for label, value in facts)
            + f"\n\n{decision}. {summary}\n"
            + "\n".join(f"{label}: {outcome} — {evidence}" for label, outcome, evidence in rows)
        )
    note = (
        "Transport figures are from the listing. Straight-line distance is not walking distance. "
        "Evidence may be shortened; the archive keeps the complete review."
    )
    document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:20px;font-family:Arial,sans-serif;color:#1f2937">'
        '<div style="max-width:780px;margin:auto">'
        + "".join(sections)
        + f'<p style="font-size:11px;color:#666;margin-top:22px">{html.escape(note)}</p>'
        + "</div></body></html>"
    )
    return EmailDigest(
        subject, document, "\n\n".join(text_sections) + f"\n\n{note}\n", property_ids
    )
