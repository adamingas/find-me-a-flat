"""Preserve useful source evidence when rendering the readable review dossier."""

from copy import deepcopy
from types import SimpleNamespace

from openrent.review_prompt import render_listing


def test_readable_dossier_preserves_source_facts_units_transport_and_complete_gallery():
    description = (
        "First paragraph.\n" + "Full original description — ample daylight. " * 1600 + "\nEND"
    )
    snapshot = {
        "id": 101,
        "title": "Studio on Example Street",
        "url": "https://www.openrent.co.uk/property-to-rent/london/101",
        "address_display": "Example Street",
        "locality": "London",
        "postcode": "SW1V 1AA",
        "country": "GB",
        "latitude": 51.49,
        "longitude": -0.14,
        "rent_pcm_pence": 190_025,
        "rent_weekly_pence": 43_852,
        "deposit_pence": 190_025,
        "currency": "GBP",
        "bedrooms": 0,
        "bathrooms": 1,
        "available_from": "2026-10-03",
        "minimum_tenancy_months": 6,
        "furnishing": "Furnished",
        "garden": False,
        "pets_allowed": None,
        "description": description,
        "source_html": "SOURCE_HTML_SENTINEL",
        "description_html": "DESCRIPTION_HTML_SENTINEL",
        "updated_at": "ARCHIVE_TIMESTAMP_SENTINEL",
        "property_type_code": "PRIVATE_TYPE_CODE_SENTINEL",
        "features": [
            {"label": "Bills included", "value_type": "boolean", "value_boolean": 0},
            {"label": "Extra rooms", "value_type": "integer", "value_integer": 0},
            {"label": "Floor area", "value_type": "real", "value_real": 44.5, "unit": "m²"},
            {"label": "Lift access", "value_type": "text", "value_text": "Ground-floor entry"},
            {"label": "Window orientation", "value_type": None},
            {
                "label": "Holding deposit",
                "value_type": "integer",
                "value_integer": 43_852,
                "unit": "pence",
            },
            {"feature_key": "streetview_heading", "label": "CAMERA_CONTROL_SENTINEL", "value": 12},
            {
                "feature_key": "email_address_verified",
                "label": "CONTACT_VERIFICATION_SENTINEL",
                "value": True,
            },
        ],
        "nearby_places": [
            {"name": "Victoria", "kind": "underground", "walking_minutes": 0, "distance_km": 0},
            {
                "name": "Victoria Rail",
                "kind": "national_rail",
                "walking_minutes": None,
                "distance_km": 0.6,
            },
            {"name": "School on Example Road", "kind": "school", "walking_minutes": 4},
        ],
        "media_links": [
            {"kind": "video", "caption": "Walkthrough", "url": "https://example.com/tour"}
        ],
        "search": {"location": "Victoria Station, London", "distance_km": 0, "commute_minutes": 0},
        "images": [
            {"kind": "photo", "caption": "From archive", "content_sha256": "IMAGE_HASH_SENTINEL"},
            {"kind": "floorplan", "caption": "Original layout"},
            {"kind": "map", "caption": "Location map"},
        ],
    }
    images = [
        SimpleNamespace(
            content=b"ORIGINAL_IMAGE_BYTES_SENTINEL",
            content_type="image/jpeg",
            kind="photo",
            caption="Living room",
        ),
        SimpleNamespace(content=b"floorplan", content_type="image/png"),
        SimpleNamespace(content=b"map", content_type="image/png"),
    ]
    original = deepcopy(snapshot)
    prompt = render_listing(snapshot, images)
    for fact in (
        "Studio on Example Street",
        snapshot["url"],
        "Example Street, London, SW1V 1AA, United Kingdom",
        "£1,900.25",
        "£438.52",
        "Bedrooms: 0",
        "Garden: No",
        "Pets allowed: Not supplied",
        "2026-10-03",
        "6 months",
        "Bills included: No",
        "Extra rooms: 0",
        "Floor area: 44.5 m²",
        "Lift access: Ground-floor entry",
        "Window orientation: Not supplied",
        "Holding deposit: £438.52",
        "Victoria (Underground): 0 minutes' walk; 0 km",
        "Victoria Rail (National Rail): walking time not supplied; 0.6 km",
        "School on Example Road (School): 4 minutes' walk",
        "OpenRent's displayed figures, not independently verified",
        "Walkthrough: https://example.com/tour",
        "Search centred on: Victoria Station, London",
        "Search-reported distance from the centre: 0 km",
        "Search-reported commute time: 0 minutes",
        "3 images supplied",
        "Image 1: Photograph — Living room",
        "Image 2: Floorplan — Original layout",
        "Image 3: Map — Location map",
    ):
        assert fact in prompt, fact
    assert description in prompt and description.endswith("\nEND")
    assert "SENTINEL" not in prompt
    assert snapshot == original
    assert images[0].content == b"ORIGINAL_IMAGE_BYTES_SENTINEL"


def test_sparse_and_unfamiliar_facts_remain_labelled_prose_without_invented_values():
    snapshot = {
        "id": 2,
        "bedrooms": None,
        "garden": None,
        "extra_metadata": {
            "ceiling_height_m": 3.2,
            "view": {"river": True, "roads": None},
            "floors": ["ground", "mezzanine"],
        },
        "new_source_fact": {"access": {"stairs": 0, "lift": False}},
    }
    original = deepcopy(snapshot)
    prompt = render_listing(snapshot, [])
    for fact in (
        "Rent per month: Not supplied",
        "Bedrooms: Not supplied",
        "Garden: Not supplied",
        "Furnishing: Not supplied",
        "Ceiling height m: 3.2",
        "River: Yes",
        "Roads: Not supplied",
        "Item 1: ground",
        "Item 2: mezzanine",
        "New source fact:",
        "Stairs: 0",
        "Lift: No",
        "0 images supplied",
    ):
        assert fact in prompt, fact
    assert not any(character in prompt for character in '{}[]"')
    assert "£0" not in prompt and "Bedrooms: 0" not in prompt
    assert snapshot == original
