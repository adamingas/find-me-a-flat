"""Filter boundaries that matter when OpenRent supplies an unfiltered superset."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from openrent.models import Candidate, Property, SearchData
from openrent.parsing import parse_search
from openrent.search import (
    ApiFilters,
    SearchError,
    WebsiteFilters,
    distance_km,
    matches_criteria,
)


def filters(**fields):
    api = ApiFilters(
        **{name: fields.pop(name) for name in ApiFilters.__dataclass_fields__ if name in fields}
    )
    fields.setdefault("include_unavailable", False)
    return api, WebsiteFilters(**fields)


def candidate(**fields):
    values = {
        "id": 123,
        "url": "https://www.openrent.co.uk/123",
        "is_live": True,
        "rent_pcm_pence": 150_025,
        "bedrooms": 2,
        "bathrooms": 1,
        "property_type_code": 2,
        "is_shared": False,
        "is_studio": False,
    }
    values.update(fields)
    return Candidate(Property(**values), distance_km=1.0)


def options(**fields):
    return filters(location="London", radius_distance=2.0, **fields)


def test_distance_uses_coordinates_instead_of_rounded_display_value():
    source = SearchData([], latitude=51.5, longitude=-0.1)
    item = candidate(latitude=51.509, longitude=-0.1)
    item.distance_km = 0.99  # The source's rounded display would pass a 1 km limit.
    opts = filters(location="London", radius_distance=1)
    assert not matches_criteria(*opts, item, source)
    assert item.distance_km == pytest.approx(distance_km(51.5, -0.1, 51.509, -0.1))
    assert item.distance_km > 1


def test_exact_distance_limit_is_inclusive_and_missing_distance_is_an_error():
    item = candidate()
    opts = filters(location="London", radius_distance=1)
    assert matches_criteria(*opts, item, SearchData([]))
    item.distance_km = 1.00001
    assert not matches_criteria(*opts, item, SearchData([]))
    item.distance_km = None
    with pytest.raises(SearchError, match="no usable location or distance"):
        matches_criteria(*opts, item, SearchData([]))


def test_commute_boundary_is_inclusive_and_never_uses_distance_fallback():
    opts = filters(location="London", radius_minutes=30)
    item = candidate()
    item.commute_minutes = 30
    assert matches_criteria(*opts, item, SearchData([], distance_unit="minutes"))
    item.commute_minutes = 30.01
    assert not matches_criteria(*opts, item, SearchData([], distance_unit="minutes"))
    item.commute_minutes = None
    with pytest.raises(SearchError, match="refusing a distance fallback"):
        matches_criteria(*opts, item, SearchData([], distance_unit="km"))


def test_minimum_tenancy_filter_is_a_ceiling_and_move_in_is_inclusive():
    opts = options(max_minimum_tenancy=6, move_in_before=date(2026, 10, 15))
    source = SearchData([])
    assert matches_criteria(
        *opts, candidate(minimum_tenancy_months=6, available_from="2026-10-15"), source
    )
    assert matches_criteria(
        *opts, candidate(minimum_tenancy_months=0, available_from="2026-10-01"), source
    )
    assert not matches_criteria(
        *opts, candidate(minimum_tenancy_months=12, available_from="2026-10-01"), source
    )
    assert not matches_criteria(
        *opts, candidate(minimum_tenancy_months=6, available_from="2026-10-16"), source
    )
    with pytest.raises(SearchError, match="minimum_tenancy_months"):
        matches_criteria(
            *opts, candidate(minimum_tenancy_months=None, available_from="2026-10-01"), source
        )


def test_multiple_property_types_are_combined_with_or_and_missing_type_is_an_error():
    opts = options(property_types=("house", "flat"))
    assert matches_criteria(*opts, candidate(property_type_code=1), SearchData([]))
    assert matches_criteria(*opts, candidate(property_type_code=2), SearchData([]))
    assert not matches_criteria(*opts, candidate(property_type_code=3), SearchData([]))
    with pytest.raises(SearchError, match="property_type_code"):
        matches_criteria(*opts, candidate(property_type_code=None), SearchData([]))


def test_bedroom_limits_require_only_the_facts_needed_for_effective_count():
    source = SearchData([])
    assert matches_criteria(
        *options(bedrooms_max=0), candidate(is_shared=True, is_studio=None, bedrooms=None), source
    )
    assert matches_criteria(
        *options(bedrooms_min=0, bedrooms_max=0),
        candidate(is_shared=False, is_studio=True, bedrooms=None),
        source,
    )
    with pytest.raises(SearchError, match="is_shared"):
        matches_criteria(
            *options(bedrooms_max=0), candidate(is_shared=None, is_studio=True), source
        )


def test_missing_requested_data_is_validated_before_known_nonmatches_criteria():
    # Filter order must not hide source drift behind another candidate exclusion.
    with pytest.raises(SearchError, match="pets_allowed"):
        matches_criteria(
            *options(pets=True), candidate(is_live=False, pets_allowed=None), SearchData([])
        )


def test_missing_optional_search_array_cannot_silently_become_no_matches_criteria():
    html = (Path(__file__).parent / "fixtures" / "search.html").read_text()
    search = parse_search(html.replace("var pets =", "var renamedPets ="))
    with pytest.raises(SearchError, match="pets_allowed.*--pets"):
        [item for item in search.candidates if matches_criteria(*options(pets=True), item, search)]


def test_invalid_move_in_date_cannot_silently_exclude_a_listing():
    with pytest.raises(SearchError, match="invalid available_from.*--move-in-before"):
        matches_criteria(
            *options(move_in_before=date(2026, 10, 15)),
            candidate(available_from="not-a-date"),
            SearchData([]),
        )


def test_only_geography_is_sent_and_model_defaults_are_unconstrained():
    from dataclasses import fields

    # Model defaults impose no constraint; the CLI deliberately defaults to live-only.
    for model in (ApiFilters, WebsiteFilters):
        assert all(getattr(model(), field.name) is None for field in fields(model))
    default = filters(location="Victoria Station, London", radius_distance=2)
    assert default[0].parameters() == {
        "term": "Victoria Station, London",
        "searchType": "km",
        "area": "3",
    }
    rich = options(
        rent_min=Decimal("1200.50"),
        rent_max=Decimal("2000.75"),
        pets=True,
        furnishing="unfurnished",
        no_shared=True,
        no_studios=True,
        move_in_before=date(2026, 10, 15),
        max_minimum_tenancy=6,
        property_types=("house", "flat"),
    )
    assert rich[0].parameters() == default[0].parameters() | {"term": "London"}
    assert "acceptPets" not in rich[0].parameters()
    assert matches_criteria(ApiFilters(), WebsiteFilters(), candidate(is_live=None), SearchData([]))


def test_server_radius_is_an_integer_superset_of_precise_distance_or_time():
    opts = filters(location=" London ", radius_distance=1.5)
    assert opts[0].parameters()["term"] == "London"
    assert int(opts[0].parameters()["area"]) > opts[0].radius_km
    miles = filters(location="London", radius_distance=2, distance_unit="miles")
    assert miles[0].radius_km == pytest.approx(3.218688)
    assert int(miles[0].parameters()["area"]) > miles[0].radius_km
    commute = filters(location="London", radius_minutes=15)
    assert commute[0].parameters()["searchType"] == "minutes"
    assert int(commute[0].parameters()["area"]) > 15  # Native endpoint excludes equal minutes.
