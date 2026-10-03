import json
from datetime import date
from pathlib import Path

import pytest

from openrent.models import Candidate, Property
from openrent.parsing import ParseError, enrich_summary, parse_property, parse_search

FIXTURES = Path(__file__).parent / "fixtures"


def search_html() -> str:
    return (FIXTURES / "search.html").read_text()


def test_live_search_arrays_store_typed_metadata_and_response_date():
    data = parse_search(search_html(), reference_date=date(2026, 10, 1))
    assert data.total == 3
    assert data.location == "London"
    assert data.latitude == 51.50735
    first, studio, third = data.candidates
    prop = first.property
    assert prop.id == 2865841
    assert prop.rent_pcm_pence == 285000
    assert prop.bedrooms == prop.bathrooms == 1
    assert prop.available_from == "2027-01-06"
    assert prop.first_listed_at == "2026-04-17T09:21:14.333000+00:00"
    assert prop.furnished is True and prop.unfurnished is False
    assert prop.bills_included is True
    assert prop.families_allowed is False
    assert prop.pets_allowed is False
    assert prop.latitude == 51.50751
    assert first.distance_km == 0.21
    assert first.commute_minutes is None
    # OpenRent's bedrooms array reports 1 for studios; retain its actual value.
    assert studio.property.is_studio is True
    assert studio.property.bedrooms == 1
    assert third.property.available_from == "2027-02-15"


def test_minutes_are_commute_not_distance():
    html = search_html().replace('" km"', '" minutes"')
    data = parse_search(html)
    assert data.candidates[0].commute_minutes == 0.21
    assert data.candidates[0].distance_km is None


def test_miles_are_converted_to_kilometers():
    html = search_html().replace('" km"', '" miles"')
    assert parse_search(html).candidates[0].distance_km == pytest.approx(0.21 * 1.609344)


def test_js_literals_trailing_comma_and_boolean_constants_are_safe():
    html = search_html().replace("[2865841, 2870098, 2940468]", "[2865841, 2870098, 2940468,]")
    html = html.replace("var pets = [0, 0, 0]", "var pets = [false, false, true,]")
    data = parse_search(html)
    assert data.candidates[2].property.pets_allowed is True
    bad = html.replace("var pets = [false, false, true,]", "var pets = [false, false, evil()]")
    with pytest.raises(ParseError, match="unsupported JavaScript expression"):
        parse_search(bad)


def test_summary_api_merges_and_preserves_precision():
    candidate = parse_search(search_html()).candidates[0]
    summary = json.loads((FIXTURES / "summaries.json").read_text())[0]
    summary["newFlexibleScalar"] = "hello"
    summary["newNestedSchema"] = {"options": [1, 2]}
    assert enrich_summary(candidate, summary) is candidate
    prop = candidate.property
    assert prop.url == "https://www.openrent.co.uk/2865841"
    assert prop.rent_weekly_pence == 65769
    assert prop.title == "1 Bed Flat, London, WC2N"
    assert prop.images[0].source_url.endswith(".JPG")
    assert "homepage" not in prop.images[0].source_url
    assert prop.extra_metadata == {"summary": {"newNestedSchema": {"options": [1, 2]}}}
    assert (
        next(feature for feature in prop.features if feature.label == "newFlexibleScalar").value
        == "hello"
    )
    with pytest.raises(ParseError, match="does not match"):
        enrich_summary(candidate, {"id": 1})


def test_live_detail_normalizes_all_listing_fields_and_original_media():
    candidate = parse_search(search_html(), reference_date=date(2026, 10, 1)).candidates[0]
    prop = parse_property((FIXTURES / "detail.html").read_text(), candidate.property.url, candidate)
    assert prop.id == 2865841
    assert prop.detail_complete is True
    assert candidate.property.detail_complete is False
    assert prop.url.endswith("/london/1-bed-flat-london-wc2n/2865841")
    assert prop.rent_pcm_pence == prop.deposit_pence == 285000
    assert prop.rent_weekly_pence == 65769
    assert prop.bedrooms == prop.bathrooms == prop.max_tenants == 1
    assert prop.latitude == 51.50751
    assert prop.longitude == -0.1246827
    assert prop.locality == "London"
    assert prop.postcode == "WC2N 5NP"
    assert prop.address_display == "London, WC2N"
    assert prop.available_from == "2027-01-06"
    assert prop.bills_included is True
    assert prop.pets_allowed is False
    assert prop.video_viewings is True
    assert prop.epc_rating == "C"
    assert prop.landlord_name == "Naza B."
    assert prop.landlord_member_since == "March 2025"
    assert prop.status == "available"
    assert "Spacious & Stylish" in prop.description
    assert "<p>" in prop.description_html
    assert len(prop.images) == 3  # Two full-size originals, plus a map.
    assert prop.images[0].width == 1440
    assert prop.images[0].height == 960
    assert prop.images[-1].kind == "map"
    assert len(prop.media_links) == 1
    assert prop.media_links[0].kind == "youtube"
    assert prop.nearby_places[0].name == "London Charing Cross"
    assert prop.nearby_places[0].walking_minutes == 1
    feature = next(feature for feature in prop.features if feature.key == "dss_lha_covers_rent")
    assert feature.label == "DSS/LHA Covers Rent"
    assert feature.value is False
    assert all("broad rental market" not in feature.label for feature in prop.features)
    assert (
        next(feature for feature in prop.features if feature.key == "streetview_heading").value
        == 90.68705
    )


def test_detail_requires_correct_identity_and_listing():
    html = (FIXTURES / "detail.html").read_text()
    with pytest.raises(ParseError, match="does not match"):
        parse_property(
            html,
            "https://www.openrent.co.uk/1",
            Candidate(Property(1, "https://www.openrent.co.uk/1")),
        )
    with pytest.raises(ParseError, match="Missing listing title"):
        parse_property("<h1>Please log in</h1>", "https://www.openrent.co.uk/1")


def test_detail_without_search_candidate_retains_responsive_bed_and_bath_labels():
    prop = parse_property(
        (FIXTURES / "detail.html").read_text(), "https://www.openrent.co.uk/2865841"
    )
    assert prop.bedrooms == prop.bathrooms == prop.max_tenants == 1


def test_sanitized_source_does_not_store_credential_tokens_or_scripts():
    html = (
        (FIXTURES / "detail.html")
        .read_text()
        .replace(
            "<main>",
            '<main data-csrf="secret" onclick="alert(1)"><script>token="secret";</script>'
            '<form><input name="__RequestVerificationToken" value="secret"></form>',
        )
    )
    prop = parse_property(html, "https://www.openrent.co.uk/2865841")
    assert "secret" not in prop.source_html
    assert "<script" not in prop.source_html
    assert "onclick" not in prop.source_html
    assert "data-csrf" not in prop.source_html


def test_legacy_table_tenancy_and_icon_markup():
    html = """<h1>2 Bed Flat, Example Road, CB1</h1><div id="description">Nice flat</div>
    <h2>Details</h2><table>
    <tr><td>Rent PCM</td><td>£1,234.56</td></tr>
    <tr><td>Preferred Minimum Tenancy</td><td>1 year</td></tr>
    <tr><td>Maximum Tenancy</td><td>18 months</td></tr>
    <tr><td>Garden Access</td><td><i class="glyphicon-ok"></i></td></tr>
    <tr><td>Furnishing</td><td>Furnished or Unfurnished</td></tr>
    <tr><td>Unusual Scalar</td><td>Variable</td></tr></table>
    <script>var x = new google.maps.LatLng(52.2, 0.1);</script>"""
    prop = parse_property(html, "https://www.openrent.co.uk/1234")
    assert prop.rent_pcm_pence == 123456
    assert prop.bedrooms == 2
    assert prop.minimum_tenancy_months == 12
    assert prop.maximum_tenancy_months == 18
    assert prop.garden is True
    assert prop.furnished is True and prop.unfurnished is True
    assert prop.latitude == 52.2 and prop.longitude == 0.1
    assert prop.extra_metadata == {}
    assert (
        next(feature for feature in prop.features if feature.key == "unusual_scalar").value
        == "Variable"
    )


def test_tenant_choice_preserves_both_furnishing_options_and_street_address():
    candidate = Candidate(
        Property(2623419, "https://www.openrent.co.uk/2623419", furnished=True, unfurnished=True)
    )
    html = """<main><h1>2 Bed Flat, Honey Hill Mews, CB3</h1><table>
    <tr><td>Rent PCM</td><td>£1,500</td></tr>
    <tr><td>Furnishing</td><td>At tenant choice</td></tr></table>
    <div id="map" data-lat="52.2" data-lng="0.1"><h2>Cambridge, CB3</h2></div></main>"""
    prop = parse_property(html, candidate.property.url, candidate)
    assert prop.furnished is True
    assert prop.unfurnished is True
    assert prop.address_display == "Honey Hill Mews, CB3"
    assert prop.locality == "Cambridge"
    assert (
        next(feature for feature in prop.features if feature.key == "region_display").value
        == "Cambridge, CB3"
    )


def test_generated_source_element_ids_do_not_create_changes_on_every_fetch():
    html = (FIXTURES / "detail.html").read_text()
    first_id = "OR00000000000000000000000000000000"
    second_id = "OR11111111111111111111111111111111"
    html = html.replace("<main>", f'<main id="{first_id}" data-bs-content-id="{first_id}">')
    first = parse_property(html, "https://www.openrent.co.uk/2865841")
    second = parse_property(html.replace(first_id, second_id), "https://www.openrent.co.uk/2865841")
    assert first.source_html == second.source_html


def test_gallery_explicit_image_type_is_archived_as_an_image():
    html = """<main><h1>1 Bed Flat, Example Road, CB1</h1>
    <table><tr><td>Rent PCM</td><td>£1,000</td></tr></table>
    <a class="lightbox_item" href="//imagescdn.openrent.co.uk/floorplan.png"
       data-pswp-type="image" data-pswp-width="600" data-pswp-height="400"
       data-pswp-caption="Floor plan"></a></main>"""
    prop = parse_property(html, "https://www.openrent.co.uk/1234")
    assert len(prop.images) == 1
    assert prop.images[0].source_url == "https://imagescdn.openrent.co.uk/floorplan.png"
    assert prop.images[0].caption == "Floor plan"
    assert prop.media_links == []
