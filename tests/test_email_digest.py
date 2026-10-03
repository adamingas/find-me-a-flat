"""Digests preserve every fixed assessment and never turn archive text into markup."""

from copy import deepcopy

from bs4 import BeautifulSoup

from openrent.email_digest import DigestListing, render_digest


def _result():
    return {
        "decision": "pass",
        "summary": "Good candidate; check the bedroom dimensions.",
        "no_living_room_carpet": {"outcome": "met", "evidence": "Wood flooring in image 1."},
        "bathroom_without_window": {"outcome": "not_met", "evidence": "Window in image 2."},
        "kitchen_counter_space_for_four_appliances": {"outcome": "unknown", "evidence": "Check."},
        "gas_hob_or_induction_stovetop": {"outcome": "met", "evidence": "Induction hob."},
        "bedroom_carpet": {"outcome": "met", "evidence": "Bedroom photograph."},
        "area_at_least_50_m2": {"outcome": "met", "evidence": "Floor plan states 72.5 m²."},
        "primary_bedroom_fits_super_king_bed": {"outcome": "met", "evidence": "3.5 × 4 m room."},
        "not_ground_floor": {"outcome": "not_met", "evidence": "Ground-floor description."},
        "area_m2": {"value": 72.5, "evidence": "Floor plan."},
        "floor": {"value": 0.0, "evidence": "Street-level windows.", "certainty": "medium"},
        "images_examined": [1, 2, 3],
    }


def test_digest_renders_original_metrics_all_assessments_and_numeric_provenance():
    result = _result()
    data = {
        "rent_pcm_pence": 190_025,
        "bedrooms": 0,
        "postcode": "SW1V 1AA",
        "latitude": 51.4965,
        "longitude": -0.1447,
        "nearby_places": [
            {"name": "Pimlico", "kind": "underground", "walking_minutes": 9, "distance_km": 0.6},
            {"name": "Victoria", "kind": "underground", "walking_minutes": 0, "distance_km": 0},
            {"name": "A rail station", "kind": "national_rail", "walking_minutes": 0},
        ],
    }
    before = deepcopy((data, result))
    digest = render_digest([DigestListing(1, "A flat", "https://openrent.co.uk/1", data, result)])
    assert digest.subject == "1 new flats found"
    assert digest.property_ids == (1,)
    for fragment in (
        "£1,900.25 / month",
        "Bedrooms: 0",
        "Postcode: SW1V 1AA",
        "Victoria — 0 km; 0 min walk",
        "Distance to Victoria: 0 km; 0 min walk (listing)",
        "No living-room carpet (required): True",
        "Bathroom has no window: False",
        "Counter space for four appliances: Unknown",
        "Gas hob or induction stovetop: True",
        "Bedroom has carpet: True",
        "At least 50 m² (required): True",
        "Super king bed fits (1.8 × 2 m) (required): True",
        "Not ground floor: False",
        "Area: 72.5 m² · Stated",
        "Floor (ground = 0): 0 · Estimated (medium)",
    ):
        assert fragment in digest.text, fragment
    assert "<table" in digest.html and 'href="https://openrent.co.uk/1"' in digest.html
    assert "Images examined" not in digest.html and "Images examined" not in digest.text
    assert (data, result) == before


def test_untrusted_content_is_escaped_and_unknown_distances_are_not_invented():
    result = _result()
    result["summary"] = '<script>alert("summary")</script>'
    result["no_living_room_carpet"]["evidence"] = '<img src=x onerror="alert(1)"> ' * 100
    result["area_m2"] = None
    result["floor"] = {"value": -1.0, "evidence": "Basement explicitly stated."}
    listings = [
        DigestListing(1, "<script>Title</script>", "javascript:alert(1)", {}, result),
        DigestListing(
            2,
            "Second flat",
            'https://example.org/flat?q="onclick=alert(1)',
            {"latitude": 51.4965, "longitude": -0.1447},
            result,
        ),
    ]
    digest = render_digest(listings, victoria_coordinates=(51.4965, -0.1447))
    assert digest.subject == "2 new flats found"
    assert digest.property_ids == (1, 2)
    assert "<script>" not in digest.html and "<img" not in digest.html
    assert "&lt;script&gt;Title&lt;/script&gt;" in digest.html
    assert "javascript:" not in digest.html
    assert 'href="https://example.org/flat?q=&quot;onclick=alert(1)"' in digest.html
    assert "Nearest reported Tube: Unknown" in digest.text
    assert "Distance to Victoria: Unknown" in digest.text
    assert "Distance to Victoria: 0 km (straight-line)" in digest.text
    assert "Area: Unknown" in digest.text
    assert "Floor (ground = 0): -1 · Stated" in digest.text
    assert "…" in digest.text


def test_boolean_outcomes_and_full_ordered_gallery_have_safe_controls_and_visible_fallback():
    result = _result()
    result["no_living_room_carpet"]["outcome"] = True
    result["bathroom_without_window"]["outcome"] = False
    result["kitchen_counter_space_for_four_appliances"]["outcome"] = None
    data = {
        "images": [
            {"position": 2, "source_url": "https://example.com/map.png", "kind": "map"},
            {"position": 1, "source_url": "https://example.com/bed.jpg", "caption": "Bedroom"},
            {
                "position": 0,
                "source_url": 'https://example.com/living.jpg?x="onerror=alert(1)',
                "caption": "<script>Living room</script>",
            },
            {"position": 3, "source_url": "https://example.com/bed.jpg", "caption": "Duplicate"},
            {"position": 4, "source_url": "javascript:alert(1)"},
            {"position": 5, "source_url": "data:image/png;base64,AAAA"},
        ]
    }
    before = deepcopy((result, data))
    digest = render_digest([DigestListing(27, "A flat", "https://openrent.co.uk/27", data, result)])
    soup = BeautifulSoup(digest.html, "html.parser")
    images = soup.select(".flat-gallery img")
    assert [image["src"] for image in images] == [
        data["images"][2]["source_url"],
        "https://example.com/bed.jpg",
        "https://example.com/map.png",
    ]
    assert images[0]["alt"] == "<script>Living room</script>"
    assert all(not image.has_attr("onerror") for image in images)
    assert soup.select("script, input, form") == []
    slides = soup.select(".flat-gallery-slide")
    assert [slide["id"] for slide in slides] == [
        "flat-27-image-1",
        "flat-27-image-2",
        "flat-27-image-3",
    ]
    assert "display:none" not in str(slides[0])
    assert slides[0].select_one("figcaption a")["href"] == "#flat-27-image-3"
    assert slides[-1].select("figcaption a")[-1]["href"] == "#flat-27-image-1"
    assert soup.find("a", string="View all listing images on OpenRent")["href"] == (
        "https://openrent.co.uk/27"
    )
    assert "No living-room carpet (required): True" in digest.text
    assert "Bathroom has no window: False" in digest.text
    assert "Counter space for four appliances: Unknown" in digest.text
    assert "Gallery: 3 archived images" in digest.text
    assert "Images examined" not in digest.html + digest.text
    assert "javascript:" not in digest.html and "base64" not in digest.html
    assert (result, data) == before


def test_gallery_without_safe_images_keeps_listing_fallback_and_never_embeds_invalid_urls():
    digest = render_digest(
        [
            DigestListing(
                1,
                "A flat",
                "https://openrent.co.uk/1",
                {"images": [{"source_url": "file:///private/tmp/image.png"}, None]},
                _result(),
            )
        ]
    )
    soup = BeautifulSoup(digest.html, "html.parser")
    assert soup.find_all("img") == []
    assert "No archived images available" in digest.text
    assert soup.find("a", string="View all listing images on OpenRent")["href"] == (
        "https://openrent.co.uk/1"
    )
    assert "file:" not in digest.html
