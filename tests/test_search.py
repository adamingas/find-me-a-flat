"""Filter boundaries that matter when OpenRent supplies an unfiltered superset."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from openrent.models import Candidate, Property, SearchData
from openrent.parsing import parse_search
from openrent.search import SearchError, SearchOptions, distance_km


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
    return SearchOptions(location="London", radius_distance=2.0, **fields)


def test_distance_uses_coordinates_instead_of_rounded_display_value():
    source = SearchData([], latitude=51.5, longitude=-0.1)
    item = candidate(latitude=51.509, longitude=-0.1)
    item.distance_km = 0.99  # The source's rounded display would pass a 1 km limit.
    opts = SearchOptions(location="London", radius_distance=1)
    assert not opts.matches(item, source)
    assert item.distance_km == pytest.approx(distance_km(51.5, -0.1, 51.509, -0.1))
    assert item.distance_km > 1


def test_exact_distance_limit_is_inclusive_and_missing_distance_is_an_error():
    item = candidate()
    opts = SearchOptions(location="London", radius_distance=1)
    assert opts.matches(item, SearchData([]))
    item.distance_km = 1.00001
    assert not opts.matches(item, SearchData([]))
    item.distance_km = None
    with pytest.raises(SearchError, match="no usable location or distance"):
        opts.matches(item, SearchData([]))


def test_commute_boundary_is_inclusive_and_never_uses_distance_fallback():
    opts = SearchOptions(location="London", radius_minutes=30)
    item = candidate()
    item.commute_minutes = 30
    assert opts.matches(item, SearchData([], distance_unit="minutes"))
    item.commute_minutes = 30.01
    assert not opts.matches(item, SearchData([], distance_unit="minutes"))
    item.commute_minutes = None
    with pytest.raises(SearchError, match="refusing a distance fallback"):
        opts.matches(item, SearchData([], distance_unit="km"))


def test_minimum_tenancy_filter_is_a_ceiling_and_move_in_is_inclusive():
    opts = options(max_minimum_tenancy=6, move_in_before=date(2026, 10, 15))
    source = SearchData([])
    assert opts.matches(candidate(minimum_tenancy_months=6, available_from="2026-10-15"), source)
    assert opts.matches(candidate(minimum_tenancy_months=0, available_from="2026-10-01"), source)
    assert not opts.matches(
        candidate(minimum_tenancy_months=12, available_from="2026-10-01"), source
    )
    assert not opts.matches(
        candidate(minimum_tenancy_months=6, available_from="2026-10-16"), source
    )
    with pytest.raises(SearchError, match="minimum_tenancy_months"):
        opts.matches(candidate(minimum_tenancy_months=None, available_from="2026-10-01"), source)


def test_multiple_property_types_are_combined_with_or_and_missing_type_is_an_error():
    opts = options(property_types=("house", "flat"))
    assert opts.matches(candidate(property_type_code=1), SearchData([]))
    assert opts.matches(candidate(property_type_code=2), SearchData([]))
    assert not opts.matches(candidate(property_type_code=3), SearchData([]))
    with pytest.raises(SearchError, match="property_type_code"):
        opts.matches(candidate(property_type_code=None), SearchData([]))


def test_bedroom_limits_require_only_the_facts_needed_for_effective_count():
    source = SearchData([])
    assert options(bedrooms_max=0).matches(
        candidate(is_shared=True, is_studio=None, bedrooms=None), source
    )
    assert options(bedrooms_min=0, bedrooms_max=0).matches(
        candidate(is_shared=False, is_studio=True, bedrooms=None), source
    )
    with pytest.raises(SearchError, match="is_shared"):
        options(bedrooms_max=0).matches(candidate(is_shared=None, is_studio=True), source)


def test_missing_requested_data_is_validated_before_known_nonmatches():
    # Filter order must not hide source drift behind another candidate exclusion.
    with pytest.raises(SearchError, match="pets_allowed"):
        options(pets=True).matches(candidate(is_live=False, pets_allowed=None), SearchData([]))


def test_missing_optional_search_array_cannot_silently_become_no_matches():
    html = (Path(__file__).parent / "fixtures" / "search.html").read_text()
    search = parse_search(html.replace("var pets =", "var renamedPets ="))
    with pytest.raises(SearchError, match="pets_allowed.*--pets"):
        [item for item in search.candidates if options(pets=True).matches(item, search)]


def test_invalid_move_in_date_cannot_silently_exclude_a_listing():
    with pytest.raises(SearchError, match="invalid available_from.*--move-in-before"):
        options(move_in_before=date(2026, 10, 15)).matches(
            candidate(available_from="not-a-date"), SearchData([])
        )


def test_native_parameter_names_and_local_only_filters():
    opts = options(
        rent_min=Decimal("1200.50"),
        rent_max=Decimal("2000.75"),
        pets=True,
        students=True,
        professionals=True,
        families=True,
        dss=True,
        bills_included=True,
        garden=True,
        parking=True,
        fireplace=True,
        video=True,
        furnishing="unfurnished",
        no_shared=True,
        no_studios=True,
        move_in_before=date(2026, 10, 15),
        max_minimum_tenancy=6,
        property_types=("house", "flat"),
    )
    params = opts.parameters()
    assert params["prices_min"] == "1200.50"
    assert params["prices_max"] == "2000.75"
    for native in (
        "acceptPets",
        "acceptStudents",
        "acceptNonStudents",
        "acceptFamilies",
        "rentCoveredByDSSorPreferred",
        "includeBills",
        "hasGarden",
        "hasParking",
        "hasFireplace",
        "videoTour",
    ):
        assert params[native] == "true"
    assert params["availableBefore"] == "2026-10-15"
    assert params["minTenancy"] == "6"
    assert params["furnishedType"] == "2"
    assert (
        not {"acceptDSS", "billsIncluded", "moveInBefore", "propertyTypes", "shared", "studio"}
        & params.keys()
    )
    assert "propertyType" not in params  # No unsupported comma-separated native flag.
    identity = opts.identity_parameters()
    assert identity["requestedPropertyTypes"] == "flat,house"
    assert identity["excludeShared"] == "true"
    assert identity["excludeStudios"] == "true"
    assert options(property_types=("flat",)).parameters()["propertyType"] == "2"


def test_server_radius_is_an_integer_superset_of_precise_distance_or_time():
    opts = SearchOptions(location=" London ", radius_distance=1.5)
    assert opts.parameters()["term"] == "London"
    assert int(opts.parameters()["area"]) > opts.radius_km
    assert opts.identity_parameters()["requestedRadiusKm"] == "1.5"
    miles = SearchOptions(location="London", radius_distance=2, distance_unit="miles")
    assert miles.radius_km == pytest.approx(3.218688)
    assert int(miles.parameters()["area"]) > miles.radius_km
    commute = SearchOptions(location="London", radius_minutes=15)
    assert commute.parameters()["searchType"] == "minutes"
    assert int(commute.parameters()["area"]) > 15  # Native endpoint excludes equal minutes.
