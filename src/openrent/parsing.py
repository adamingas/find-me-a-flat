"""Parse public OpenRent search data and listing pages without executing JavaScript."""

from __future__ import annotations

import ast
import copy
import html as html_module
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .models import Candidate, Feature, Image, MediaLink, NearbyPlace, Property, SearchData


class ParseError(ValueError):
    """The response is missing expected data or contains inconsistent records."""


_ARRAY_FIELDS = {
    "prices": "rent_pcm_pence",
    "bedrooms": "bedrooms",
    "bathrooms": "bathrooms",
    "islivelistBool": "is_live",
    "students": "students_allowed",
    "nonStudents": "non_students_allowed",
    "families": "families_allowed",
    "rentCoveredDssOrPreferred": "dss_covers_rent",
    "pets": "pets_allowed",
    "isstudio": "is_studio",
    "isshared": "is_shared",
    "furnished": "furnished",
    "unfurnished": "unfurnished",
    "hasVideo": "has_video",
    "videoViewingsAccepted": "video_viewings",
    "propertyTypes": "property_type_code",
    "dateFirstListedMs": "first_listed_at",
    "gardens": "garden",
    "parkings": "parking",
    "bills": "bills_included",
    "fireplaces": "fireplace",
    "availableFrom": "available_from",
    "minimumTenancy": "minimum_tenancy_months",
    "PROPERTYLISTLATITUDES": "latitude",
    "PROPERTYLISTLONGITUDES": "longitude",
}
_BOOLEAN_FIELDS = {
    "is_live",
    "students_allowed",
    "non_students_allowed",
    "families_allowed",
    "dss_covers_rent",
    "pets_allowed",
    "is_studio",
    "is_shared",
    "furnished",
    "unfurnished",
    "has_video",
    "video_viewings",
    "garden",
    "parking",
    "bills_included",
    "fireplace",
}
_ESSENTIAL_ARRAYS = {
    "prices",
    "bedrooms",
    "bathrooms",
    "islivelistBool",
    "isstudio",
    "isshared",
    "PROPERTYLISTLATITUDES",
    "PROPERTYLISTLONGITUDES",
}


def _literal(text: str) -> Any:
    """Accept JSON/Python literals and JavaScript true/false/null only."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        tree = ast.parse(text.strip(), mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id not in {"true", "false", "null"}:
                raise ValueError("nonliteral name")

        class JsConstants(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name) -> ast.Constant:
                return ast.copy_location(
                    ast.Constant({"true": True, "false": False, "null": None}[node.id]), node
                )

        return ast.literal_eval(JsConstants().visit(tree))
    except (ValueError, SyntaxError, KeyError, RecursionError) as exc:
        raise ParseError("OpenRent data contains an unsupported JavaScript expression") from exc


def _read_literal(text: str, start: int) -> str:
    """Read one literal assignment, respecting quoted strings and nested brackets."""
    quote = None
    escaped = False
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
            if depth < 0:
                break
        elif char == ";" and depth == 0:
            return text[start:index].strip()
    raise ParseError("Unterminated OpenRent data assignment")


def _assignment(text: str, name: str) -> Any:
    match = re.search(r"(?<![\w.])" + re.escape(name) + r"\s*=\s*", text)
    if not match:
        return None
    return _literal(_read_literal(text, match.end()))


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
        if not math.isfinite(parsed) or (integer and parsed != int(parsed)):
            raise ValueError
        return int(parsed) if integer else parsed
    except (ValueError, TypeError, OverflowError) as exc:
        raise ParseError(f"Invalid numeric value in OpenRent data: {value!r}") from exc


def money_pence(value: Any) -> int | None:
    """Round advertised pounds to integer pence, never using binary float arithmetic."""
    if value is None or value == "":
        return None
    text = str(value).replace(",", "").replace("£", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ParseError(f"Invalid advertised price: {value!r}")
    try:
        return int((Decimal(match.group()) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ParseError(f"Invalid advertised price: {value!r}") from exc


def _boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if value in (0, "0", False, "false", "False"):
        return False
    if value in (1, "1", True, "true", "True"):
        return True
    raise ParseError(f"Invalid boolean value in OpenRent data: {value!r}")


def parse_search(
    html: str,
    url: str = "https://www.openrent.co.uk/find-property-to-rent-from-private-landlords",
    reference_date: date | None = None,
) -> SearchData:
    """Read aligned search arrays. Unknown/changed markup raises instead of returning no flats.

    ``availableFrom`` is an offset from the response's UK calendar date. Pass the
    HTTP Date header converted to Europe/London when replaying or caching a page.
    """
    ids = _assignment(html, "PROPERTYIDS")
    if not isinstance(ids, list):
        raise ParseError("Missing PROPERTYIDS; this is not a recognized OpenRent search response")
    arrays: dict[str, list] = {}
    for name in (*_ARRAY_FIELDS, "PROPERTYLISTCOMMUTEORDISTANCE"):
        values = _assignment(html, name)
        if values is None:
            if ids and name in _ESSENTIAL_ARRAYS:
                raise ParseError(f"Missing essential OpenRent search array: {name}")
            continue
        if not isinstance(values, list) or len(values) != len(ids):
            raise ParseError(f"OpenRent search array {name} is not aligned with PROPERTYIDS")
        arrays[name] = values
    unit = _assignment(html, "PROPERTYLISTCOMMORDISTANCEUNIT")
    if unit is None:
        unit = _assignment(html, "PROPERTYLISTCOMMUTEORDISTANCEUNIT")
    unit = str(unit).strip().lower() if unit is not None else None
    if "PROPERTYLISTCOMMUTEORDISTANCE" in arrays and unit not in {
        "km",
        "mi",
        "mile",
        "miles",
        "minute",
        "minutes",
        "min",
        "mins",
    }:
        raise ParseError(f"Unsupported OpenRent distance unit: {unit!r}")
    today = reference_date or datetime.now(ZoneInfo("Europe/London")).date()
    candidates = []
    seen = set()
    for index, raw_id in enumerate(ids):
        property_id = _number(raw_id, integer=True)
        if property_id is None or property_id <= 0 or property_id in seen:
            raise ParseError(f"Invalid or duplicate property ID in search response: {raw_id!r}")
        seen.add(property_id)
        prop = Property(id=property_id, url=urljoin(url, f"/property-to-rent/{property_id}"))
        for name, field in _ARRAY_FIELDS.items():
            if name not in arrays:
                continue
            value = arrays[name][index]
            if field in _BOOLEAN_FIELDS:
                value = _boolean(value)
            elif field == "rent_pcm_pence":
                value = money_pence(value)
            elif field in {"latitude", "longitude"}:
                value = _number(value)
            elif field == "first_listed_at":
                ms = _number(value)
                value = datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None
            elif field == "available_from":
                offset = _number(value, integer=True)
                value = (today + timedelta(days=offset)).isoformat() if offset is not None else None
            else:
                value = _number(value, integer=True)
            setattr(prop, field, value)
        if prop.latitude is None or not -90 <= prop.latitude <= 90:
            raise ParseError(f"Invalid latitude for property {property_id}")
        if prop.longitude is None or not -180 <= prop.longitude <= 180:
            raise ParseError(f"Invalid longitude for property {property_id}")
        prop.property_type = {1: "house", 2: "flat", 3: "room"}.get(prop.property_type_code)
        prop.status = "available" if prop.is_live else "let_agreed"
        if prop.furnished is not None and prop.unfurnished is not None:
            prop.furnishing = (
                "Furnished or unfurnished"
                if prop.furnished and prop.unfurnished
                else "Furnished"
                if prop.furnished
                else "Unfurnished"
                if prop.unfurnished
                else None
            )
        candidate = Candidate(prop)
        if "PROPERTYLISTCOMMUTEORDISTANCE" in arrays:
            amount = _number(arrays["PROPERTYLISTCOMMUTEORDISTANCE"][index])
            if amount is not None and amount >= 0:
                if unit in {"minute", "minutes", "min", "mins"}:
                    candidate.commute_minutes = amount
                else:
                    candidate.distance_km = amount * 1.609344 if unit != "km" else amount
        candidates.append(candidate)
    total = _number(_assignment(html, "NUMBEROFPROPERTIES"), integer=True)
    if total is None:
        raise ParseError(
            "Missing OpenRent search total; cannot verify all property IDs were returned"
        )
    if total != len(ids):
        raise ParseError("OpenRent total differs from the number of embedded property IDs")
    return SearchData(
        candidates=candidates,
        latitude=_number(_assignment(html, "SEARCHLAT")),
        longitude=_number(_assignment(html, "SEARCHLNG")),
        location=_assignment(html, "SEARCHTERM"),
        distance_unit=unit,
        total=total,
    )


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    clone = BeautifulSoup(str(node), "html.parser")
    # Responsive d-none spans contain real facts ("bedrooms", "bathrooms").
    # Hidden explanatory popovers use divs and must not become part of a label.
    for unwanted in clone.select("button, script, style, div.d-none, .popover, .sr-only"):
        unwanted.decompose()
    return " ".join(clone.get_text(" ", strip=True).split())


def _key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _add_feature(prop: Property, feature: Feature) -> None:
    # The same table can appear twice for desktop and mobile layouts.
    for index, existing in enumerate(prop.features):
        if (existing.section, existing.key) == (feature.section, feature.key):
            prop.features[index] = feature
            return
    prop.features.append(feature)


def enrich_summary(candidate: Candidate, summary: dict[str, Any]) -> Candidate:
    """Merge the site's JSON card API into a search candidate."""
    prop = candidate.property
    if _number(summary.get("id"), integer=True) != prop.id:
        raise ParseError("Card API property ID does not match its search candidate")
    prop.url = f"https://www.openrent.co.uk/{prop.id}"
    prop.title = summary.get("title") or prop.title
    prop.description = str(summary.get("description") or "").strip() or prop.description
    if summary.get("rentPerMonth") is not None:
        prop.rent_pcm_pence = money_pence(summary["rentPerMonth"])
    if summary.get("rentPerWeek") is not None:
        prop.rent_weekly_pence = money_pence(summary["rentPerWeek"])
    if "letAgreed" in summary:
        prop.is_live = not _boolean(summary["letAgreed"])
        prop.status = "available" if prop.is_live else "let_agreed"
    image_url = summary.get("imageUrl")
    if image_url and not prop.images:
        image_url = re.sub(r"_homepage\.[a-z]+$", "", str(image_url), flags=re.IGNORECASE)
        prop.images.append(Image(source_url=urljoin(prop.url, image_url)))
    for label, source, is_money in (
        ("Last Updated", "lastUpdated", False),
        ("New Listing", "isNew", False),
        ("Multiple Rooms", "isMultiRoom", False),
        ("Maximum Room Rent PCM", "maxRoomRentPerMonth", True),
        ("Maximum Room Rent Weekly", "maxRoomRentPerWeek", True),
    ):
        if source in summary:
            value = money_pence(summary[source]) if is_money else summary[source]
            _add_feature(
                prop, Feature(_key(label), label, "Summary", value, "pence" if is_money else None)
            )
    for detail in summary.get("details", []):
        text = str(detail)
        match = re.search(r"(\d+)\s*(Bed|Bath)", text, re.IGNORECASE)
        if match:
            setattr(prop, "bedrooms" if match[2].lower() == "bed" else "bathrooms", int(match[1]))
        elif "furnished" in text.lower():
            _set_furnishing(prop, text)
        else:
            _add_feature(prop, Feature(_key(text), text, "Summary", True))
    if prop.title:
        _title_fields(prop)
    recognized = {
        "i",
        "id",
        "letAgreed",
        "title",
        "description",
        "imageUrl",
        "details",
        "distance",
        "commuteTime",
        "rentPerMonth",
        "rentPerWeek",
        "lastUpdated",
        "isNew",
        "isMultiRoom",
        "maxRoomRentPerMonth",
        "maxRoomRentPerWeek",
    }
    for key, value in summary.items():
        if key not in recognized:
            if isinstance(value, (list, dict)):
                prop.extra_metadata.setdefault("summary", {})[key] = value
            else:
                _add_feature(prop, Feature(_key(key), key, "Summary", value))
    return candidate


def _set_furnishing(prop: Property, value: str) -> None:
    prop.furnishing = value
    lower = value.lower()
    if re.search(r"tenant(?:s|'s)?\s+choice", lower):
        prop.furnished = prop.unfurnished = True
        return
    if "furnished" not in lower:
        # A new display label must not erase the factual search-array flags.
        return
    prop.unfurnished = "unfurnished" in lower
    prop.furnished = "furnished" in lower.replace("unfurnished", "")


def _title_fields(prop: Property) -> None:
    title = prop.title or ""
    match = re.match(r"(\d+)\s*Bed(?:room)?\s+(.+?)(?:,|$)", title, re.IGNORECASE)
    if match:
        if prop.bedrooms is None:
            prop.bedrooms = int(match[1])
        prop.property_type = match[2].strip().lower()
    if re.search(r"\bstudio\b", title, re.IGNORECASE):
        prop.is_studio = True
        prop.property_type = "flat"
    if re.search(r"\broom in\b|\bhouse share\b|\bflat share\b", title, re.IGNORECASE):
        prop.is_shared = True
        prop.property_type = "room"
    parts = [part.strip() for part in title.split(",")]
    if len(parts) > 1:
        prop.address_display = ", ".join(parts[1:])


def _date(value: str) -> str | None:
    value = value.strip()
    for pattern in ("%d %B, %Y", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return (
                datetime.strptime(value, pattern)
                .replace(tzinfo=ZoneInfo("Europe/London"))
                .date()
                .isoformat()
            )
        except ValueError:
            continue
    if value.lower() in {"today", "now", "immediately", "available now"}:
        return datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    return None


def _cell_value(cell: Tag) -> str | bool:
    if cell.select_one(".text-success, .glyphicon-ok, .fa-check, .fa-check-circle"):
        return True
    if cell.select_one(".text-danger, .glyphicon-remove, .fa-times, .fa-times-circle"):
        return False
    value = _text(cell)
    if value.lower() in {"yes", "true"}:
        return True
    if value.lower() in {"no", "false"}:
        return False
    return value


_LABEL_BOOL_FIELDS = {
    "bills_included": "bills_included",
    "pets_allowed": "pets_allowed",
    "student_friendly": "students_allowed",
    "students_allowed": "students_allowed",
    "families_allowed": "families_allowed",
    "family_friendly": "families_allowed",
    "dss_lha_covers_rent": "dss_covers_rent",
    "dss_income_accepted": "dss_covers_rent",
    "garden": "garden",
    "garden_access": "garden",
    "parking": "parking",
    "parking_available": "parking",
    "fireplace": "fireplace",
    "smokers_allowed": "smokers_allowed",
    "online_viewings": "video_viewings",
    "video_viewings": "video_viewings",
}
_MONEY_FIELDS = {
    "deposit": "deposit_pence",
    "rent_pcm": "rent_pcm_pence",
    "rent_per_month": "rent_pcm_pence",
    "rent_pw": "rent_weekly_pence",
    "rent_per_week": "rent_weekly_pence",
}


def _parse_tables(prop: Property, scope: Tag) -> None:
    for table in scope.find_all("table"):
        heading = table.find_previous(["h2", "h3", "h4"])
        section = _text(heading)
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) != 2:
                continue
            label = _text(cells[0])
            if not label:
                continue
            key = _key(label)
            value: str | int | bool | None = _cell_value(cells[1])
            unit = None
            if key in _MONEY_FIELDS and isinstance(value, str):
                value = money_pence(value)
                unit = "pence"
                setattr(prop, _MONEY_FIELDS[key], value)
            elif key in _LABEL_BOOL_FIELDS and isinstance(value, bool):
                setattr(prop, _LABEL_BOOL_FIELDS[key], value)
            elif key == "furnishing" and isinstance(value, str):
                _set_furnishing(prop, value)
            elif key in {"available_from", "available_to_move_in"} and isinstance(value, str):
                prop.available_from = _date(value)
            elif key == "epc_rating" and isinstance(value, str):
                prop.epc_rating = value
            elif key in {
                "minimum_tenancy",
                "maximum_tenancy",
                "preferred_minimum_tenancy",
                "preferred_maximum_tenancy",
            } and isinstance(value, str):
                match = re.search(r"(\d+)\s*(months?|years?)", value, re.IGNORECASE)
                if match:
                    value = int(match[1]) * (12 if match[2].lower().startswith("year") else 1)
                    unit = "months"
                    setattr(prop, key.removeprefix("preferred_") + "_months", value)
            elif key in {"bedrooms", "bathrooms", "maximum_tenants", "max_tenants"}:
                match = re.search(r"\d+", str(value))
                if match:
                    value = int(match[0])
                    setattr(prop, "max_tenants" if "tenants" in key else key, value)
            _add_feature(prop, Feature(key, label, section, value, unit))


def _parse_gallery(prop: Property, scope: Tag) -> None:
    images, media, seen = [], [], set()
    for anchor in scope.select(
        "a.lightbox_item[href], a[data-pswp-width][href], a[data-lightbox][href]"
    ):
        url = urljoin(prop.url, str(anchor.get("href")))
        if url in seen:
            continue
        seen.add(url)
        kind = str(anchor.get("data-pswp-type", ""))
        caption = str(anchor.get("data-pswp-caption", "")) or None
        if kind.lower() not in {"", "image", "photo"} or re.search(
            r"youtube\.com|youtu\.be|vimeo\.com|\.mp4(?:\?|$)", url
        ):
            media.append(MediaLink(url=url, kind=kind or "video", caption=caption))
        else:
            images.append(
                Image(
                    source_url=url,
                    position=len(images),
                    caption=caption,
                    width=_number(anchor.get("data-pswp-width"), integer=True),
                    height=_number(anchor.get("data-pswp-height"), integer=True),
                )
            )
    if not images:
        for image in scope.select(".property-images img, .listing-image img, #propertyImages img"):
            src = image.get("data-src") or image.get("src")
            if src:
                url = urljoin(prop.url, str(src))
                if url not in seen:
                    seen.add(url)
                    images.append(Image(url, len(images), caption=image.get("alt")))
    for iframe in scope.select("iframe[src]"):
        src = str(iframe.get("src"))
        if re.search(r"youtube|vimeo", src) and urljoin(prop.url, src) not in seen:
            seen.add(urljoin(prop.url, src))
            media.append(MediaLink(urljoin(prop.url, src)))
    map_image = scope.select_one("#staticGoogleMap[src]")
    if map_image:
        images.append(
            Image(
                urljoin(prop.url, str(map_image["src"])), len(images), "map", map_image.get("alt")
            )
        )
    prop.images = images
    prop.media_links = media
    if media:
        prop.has_video = True


def _parse_map(prop: Property, soup: BeautifulSoup, scope: Tag, html: str) -> None:
    map_node = scope.select_one("#map[data-lat], [data-lat][data-lng]")
    if map_node:
        prop.latitude = _number(map_node.get("data-lat"))
        prop.longitude = _number(map_node.get("data-lng"))
        for name in (
            "streetview-lat",
            "streetview-lng",
            "streetview-zoom",
            "streetview-heading",
            "streetview-pitch",
        ):
            if map_node.has_attr("data-" + name):
                _add_feature(
                    prop,
                    Feature(
                        name.replace("-", "_"), name, "Location", _number(map_node["data-" + name])
                    ),
                )
        if map_node.has_attr("data-is-in-london"):
            _add_feature(
                prop,
                Feature(
                    "is_in_london", "In London", "Location", _boolean(map_node["data-is-in-london"])
                ),
            )
    else:
        match = re.search(
            r"(?:google\.maps\.LatLng|L\.latLng)\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)", html
        )
        if match:
            prop.latitude, prop.longitude = float(match[1]), float(match[2])
    for anchor in soup.select('a[href*="comparebroadband"]'):
        query = parse_qs(urlparse(str(anchor["href"])).query)
        for key, values in query.items():
            if key.lower() == "postcode" and values:
                prop.postcode = values[0].upper().strip()
    if map_node:
        region = map_node.find_next("h2")
        if region:
            region_display = _text(region)
            if not prop.address_display:
                prop.address_display = region_display
            if not prop.locality:
                prop.locality = region_display.split(",", 1)[0].strip() or None
            _add_feature(prop, Feature("region_display", "Region", "Location", region_display))
    # The page explicitly labels the locality separately from its masked street address.
    badges = scope.select(".listing__content > div > ul li, .property-details li")
    for badge in badges:
        value = _text(badge)
        for field, pattern in (
            ("bedrooms", r"(\d+)\s*bedrooms?"),
            ("bathrooms", r"(\d+)\s*bathrooms?"),
            ("max_tenants", r"(\d+)\s*tenants?\s*max"),
        ):
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                setattr(prop, field, int(match[1]))
                break
        else:
            if value and not re.search(r"\d", value):
                prop.locality = value


def _parse_nearby(prop: Property, scope: Tag) -> None:
    places = []
    for item in scope.find_all("li"):
        text = _text(item)
        walking = re.search(r"~?\s*(\d+)\s*min\.?\s*walk", text, re.IGNORECASE)
        distance = re.search(r"(\d+(?:\.\d+)?)\s*(km|miles?)\b", text, re.IGNORECASE)
        if not walking and not (distance and item.find("svg", attrs={"alt": "School"})):
            continue
        paragraph = item.find("p")
        if paragraph is None:
            continue
        name = " ".join(
            str(part).strip() for part in paragraph.find_all(string=True, recursive=False)
        ).strip()
        if not name:
            name = re.split(r"~?\s*\d+\s*min", text, maxsplit=1)[0].strip(" ,")
        icon = item.find(["img", "svg"], attrs={"alt": True})
        kind = str(icon.get("alt", "transport")) if icon else "transport"
        kilometers = float(distance[1]) if distance else None
        if distance and distance[2].lower() != "km":
            kilometers *= 1.609344
        places.append(
            NearbyPlace(
                name,
                kind.lower().replace(" ", "_"),
                int(walking[1]) if walking else None,
                kilometers,
            )
        )
    prop.nearby_places = places
    for node in scope.select("[x-data]"):
        expression = str(node["x-data"])
        match = re.search(r"nearbySchoolsFilter\((\[.*\])\s*,\s*\d+\)", expression)
        if match:
            data = _literal(match[1])
            if isinstance(data, list):
                prop.extra_metadata["nearby_school_attributes"] = data


def _parse_landlord(prop: Property, scope: Tag) -> None:
    heading = scope.find(["h2", "h3"], string=re.compile(r"Meet the Landlord", re.IGNORECASE))
    if heading:
        card = heading.find_parent(class_="card") or heading.parent.parent
        name = card.select_one("p.fw-medium")
        if name:
            prop.landlord_name = _text(name)
        for term in card.find_all("dt"):
            value_cell = term.find_next_sibling("dd")
            if value_cell is None:
                continue
            label, value = _text(term).strip(":"), _cell_value(value_cell)
            key = _key(label)
            if key == "member_since":
                prop.landlord_member_since = str(value)
            elif key in {"last_active", "last_seen"}:
                prop.landlord_last_active = str(value)
            _add_feature(prop, Feature(key, label, "Landlord", value))
        link = card.select_one('a[href*="landlordID="]')
        if link:
            landlord_id = parse_qs(urlparse(str(link["href"])).query).get("landlordID", [None])[0]
            _add_feature(
                prop,
                Feature(
                    "landlord_id", "Landlord ID", "Landlord", _number(landlord_id, integer=True)
                ),
            )


def _sanitize_source(scope: Tag) -> str:
    clone = BeautifulSoup(str(scope), "html.parser")
    for node in clone.select("script, style, form, input, meta"):
        node.decompose()
    for node in clone.find_all(True):
        for attr in list(node.attrs):
            if (
                attr.lower().startswith("on")
                or attr.lower().startswith("x-")
                or re.search(r"token|csrf|nonce|password", attr, re.IGNORECASE)
            ):
                del node.attrs[attr]
            elif isinstance(node.attrs[attr], str) and re.fullmatch(
                r"#?OR[0-9a-f-]{32,}", node.attrs[attr], re.IGNORECASE
            ):
                # Server-generated element IDs change on every response. Strip
                # matching ID references too, retaining stable listing selectors.
                del node.attrs[attr]
        if "class" in node.attrs:
            node.attrs["class"] = [
                name
                for name in node.attrs["class"]
                if not re.fullmatch(r"js-carousel-[0-9a-f-]{32,}", name, re.IGNORECASE)
            ]
            if not node.attrs["class"]:
                del node.attrs["class"]
    return str(clone)


def parse_property(html: str, url: str, candidate: Candidate | None = None) -> Property:
    """Parse a detail page, preserving normalized search metadata and public source HTML."""
    soup = BeautifulSoup(html, "html.parser")
    scope = soup.find("main") or soup.select_one(".listing") or soup.body or soup
    canonical = soup.select_one('link[rel="canonical"][href]')
    canonical_url = urljoin(url, str(canonical["href"])) if canonical else url
    match = re.search(r"/(\d+)(?:[/?#]|$)", urlparse(canonical_url).path)
    if match is None:
        link = soup.select_one('a[href^="/messagelandlord/"]')
        match = re.search(r"/(\d+)", str(link.get("href"))) if link else None
    if match is None and candidate is None:
        raise ParseError("Cannot identify the OpenRent property ID on this detail page")
    property_id = int(match[1]) if match else candidate.property.id
    if candidate and property_id != candidate.property.id:
        raise ParseError("Detail page property ID does not match its search candidate")
    prop = copy.deepcopy(candidate.property) if candidate else Property(property_id, canonical_url)
    prop.url = canonical_url
    title_node = scope.find("h1")
    title = _text(title_node)
    if not title:
        meta = soup.select_one('meta[name="twitter:title"]')
        title = str(meta.get("content", "")) if meta else ""
    if not title or not re.search(
        r"\b(?:bed|bedroom|studio|room|flat|house)\b", title, re.IGNORECASE
    ):
        raise ParseError("Missing listing title; this is not a recognized OpenRent detail response")
    prop.title = html_module.unescape(title)
    _title_fields(prop)
    description = scope.select_one(
        "#descriptionText > div, #descriptionText, .property-description, #description"
    )
    if description:
        prop.description = _text(description)
        prop.description_html = description.decode_contents().strip()
    _parse_tables(prop, scope)
    _parse_gallery(prop, scope)
    _parse_map(prop, soup, scope, html)
    _parse_nearby(prop, scope)
    _parse_landlord(prop, scope)
    info = scope.select_one(".listing-info, #rent, .property-rent")
    info_text = _text(info)
    for field, suffix in (
        ("rent_pcm_pence", "pcm|p/m|per month"),
        ("rent_weekly_pence", "pw|p/w|per week"),
    ):
        price = re.search(
            r"£\s*([\d,]+(?:\.\d+)?)\s*(?:" + suffix + r")\b", info_text, re.IGNORECASE
        )
        if price:
            setattr(prop, field, money_pence(price[1]))
    status_node = info.select_one(".badge") if info else None
    status_text = _text(status_node).lower().strip()
    if status_text in {"available", "let agreed", "let", "unavailable", "withdrawn"}:
        prop.status = status_text.replace(" ", "_")
        prop.is_live = status_text == "available"
    if prop.rent_pcm_pence is None:
        raise ParseError("Missing rent on OpenRent detail page")
    prop.source_html = _sanitize_source(scope)
    prop.detail_complete = True
    return prop
