"""Generated tests of our money conversion and local filters, without HTTP calls."""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openrent.cli import money
from openrent.models import Candidate, Property, SearchData
from openrent.parsing import money_pence
from openrent.search import (
    ApiFilters,
    SearchError,
    WebsiteFilters,
    matches_criteria,
    pence,
)


def filters(**fields):
    api = ApiFilters(
        **{name: fields.pop(name) for name in ApiFilters.__dataclass_fields__ if name in fields}
    )
    fields.setdefault("include_unavailable", False)
    return api, WebsiteFilters(**fields)


def pounds(cents):
    return Decimal(cents) / 100


def listing(property_id=1, distance=1.0, **facts):
    values = {
        "is_live": True,
        "is_shared": False,
        "is_studio": False,
        "rent_pcm_pence": 150_000,
        "bedrooms": 2,
        "bathrooms": 1,
    }
    values.update(facts)
    return Candidate(
        Property(id=property_id, url=f"https://www.openrent.co.uk/{property_id}", **values),
        distance_km=distance,
    )


@given(
    lower_cents=st.integers(min_value=0, max_value=5_000_000),
    width=st.integers(min_value=0, max_value=500_000),
    advertised_mills=st.integers(min_value=0, max_value=50_000_000),
)
def test_money_round_trips_pennies_and_inclusive_rent_boundaries(
    lower_cents, width, advertised_mills
):
    upper_cents = lower_cents + width
    source = SearchData([])
    options = filters(
        location="London",
        radius_distance=2,
        rent_min=pounds(lower_cents),
        rent_max=pounds(upper_cents),
    )
    for cents in (lower_cents, upper_cents):
        amount = pounds(cents)
        for representation in (amount, str(amount), float(amount)):
            assert pence(representation) == cents
        assert pence(money(str(amount))) == cents
        assert money_pence(f"£{amount:,.2f} pcm") == cents
        assert matches_criteria(*options, listing(rent_pcm_pence=cents), source)
    assert pence(lower_cents // 100) == (lower_cents // 100) * 100
    if lower_cents:
        assert not matches_criteria(*options, listing(rent_pcm_pence=lower_cents - 1), source)
    assert not matches_criteria(*options, listing(rent_pcm_pence=upper_cents + 1), source)
    with pytest.raises(SearchError, match="rent_pcm_pence"):
        matches_criteria(*options, listing(rent_pcm_pence=None), source)

    # Source adverts round half-pennies up; user constraints must be exact pennies.
    # Integer mill arithmetic supplies an independent rounding oracle.
    advertised = Decimal(advertised_mills) / 1000
    expected_cents = (advertised_mills + 5) // 10
    assert money_pence(advertised) == expected_cents
    assert money_pence(f"£{advertised:,.3f} pcm") == expected_cents
    if advertised_mills % 10:
        with pytest.raises(ValueError, match="at most 2 decimals"):
            money(str(advertised))
    else:
        assert pence(money(str(advertised))) == advertised_mills // 10


@st.composite
def nested_bounds(draw, maximum):
    """Four ordered endpoints define a wide interval and an interval inside it."""
    endpoints = draw(st.lists(st.integers(0, maximum), min_size=4, max_size=4))
    return tuple(sorted(endpoints))


@given(
    records=st.lists(
        st.tuples(
            st.integers(0, 5_000_000),
            st.integers(0, 20),
            st.integers(0, 10),
            st.booleans(),
            st.booleans(),
            st.integers(0, 1000),
        ),
        max_size=20,
    ),
    rent=nested_bounds(5_000_000),
    bedrooms=nested_bounds(20),
    bathrooms=nested_bounds(10),
    radii=st.tuples(st.integers(1, 1000), st.integers(1, 1000)),
)
def test_tightening_local_filters_on_same_candidates_never_adds_listings(
    records, rent, bedrooms, bathrooms, radii
):
    # Both filters see the same candidate snapshot; this makes no assertion
    # about inventory or results returned by different live OpenRent requests.
    wide_radius, narrow_radius = max(radii) / 100, min(radii) / 100
    common = {"location": "London"}
    wide = filters(
        **common,
        radius_distance=wide_radius,
        rent_min=pounds(rent[0]),
        rent_max=pounds(rent[3]),
        bedrooms_min=bedrooms[0],
        bedrooms_max=bedrooms[3],
        bathrooms_min=bathrooms[0],
        bathrooms_max=bathrooms[3],
    )
    narrow = filters(
        **common,
        radius_distance=narrow_radius,
        rent_min=pounds(rent[1]),
        rent_max=pounds(rent[2]),
        bedrooms_min=bedrooms[1],
        bedrooms_max=bedrooms[2],
        bathrooms_min=bathrooms[1],
        bathrooms_max=bathrooms[2],
    )
    source = SearchData([])
    items = [
        listing(
            property_id=index,
            distance=distance / 100,
            rent_pcm_pence=cents,
            bedrooms=beds,
            bathrooms=baths,
            is_shared=shared,
            is_studio=studio,
        )
        for index, (cents, beds, baths, shared, studio, distance) in enumerate(records, start=1)
    ]
    boundary = listing(
        property_id=len(items) + 1,
        distance=narrow_radius,
        rent_pcm_pence=rent[1],
        bedrooms=bedrooms[2],
        bathrooms=bathrooms[1],
    )
    items.append(boundary)
    wide_ids = {item.property.id for item in items if matches_criteria(*wide, item, source)}
    narrow_ids = {item.property.id for item in items if matches_criteria(*narrow, item, source)}
    assert boundary.property.id in narrow_ids  # The subset check always has a passing witness.
    assert narrow_ids <= wide_ids

    boundary_facts = {
        "rent_pcm_pence": rent[1],
        "bedrooms": bedrooms[2],
        "bathrooms": bathrooms[1],
    }
    for field, lower, upper in (
        ("bedrooms", bedrooms[1], bedrooms[2]),
        ("bathrooms", bathrooms[1], bathrooms[2]),
    ):
        assert not matches_criteria(
            *narrow,
            listing(distance=narrow_radius, **(boundary_facts | {field: upper + 1})),
            source,
        )
        if lower:
            assert not matches_criteria(
                *narrow,
                listing(distance=narrow_radius, **(boundary_facts | {field: lower - 1})),
                source,
            )
    assert not matches_criteria(
        *narrow, listing(distance=narrow_radius + 0.01, **boundary_facts), source
    )
    for missing_field in ("bedrooms", "bathrooms", "is_shared", "is_studio"):
        missing = listing(distance=narrow_radius, **(boundary_facts | {missing_field: None}))
        with pytest.raises(SearchError, match=missing_field):
            matches_criteria(*narrow, missing, source)

    studio = listing(
        distance=narrow_radius,
        rent_pcm_pence=rent[1],
        bedrooms=20,
        bathrooms=bathrooms[1],
        is_studio=True,
    )
    assert matches_criteria(*narrow, studio, source) is (bedrooms[1] == 0)
    shared = listing(
        distance=narrow_radius,
        rent_pcm_pence=rent[1],
        bedrooms=20,
        bathrooms=bathrooms[1],
        is_shared=True,
        is_studio=True,
    )
    assert not matches_criteria(*narrow, shared, source)  # Rooms have effective bedroom count -1.
    assert matches_criteria(*filters(**common, radius_distance=narrow_radius), shared, source)


FEATURES = {
    "pets": "pets_allowed",
    "students": "students_allowed",
    "professionals": "non_students_allowed",
    "families": "families_allowed",
    "dss": "dss_covers_rent",
    "bills_included": "bills_included",
    "garden": "garden",
    "parking": "parking",
    "fireplace": "fireplace",
}
TRISTATE = st.one_of(st.none(), st.booleans())
FLAGS = st.fixed_dictionaries(
    {
        key: st.booleans()
        for key in (*FEATURES, "video", "no_shared", "no_studios", "include_unavailable")
    }
)
FACTS = st.fixed_dictionaries(
    {
        key: TRISTATE
        for key in (
            *FEATURES.values(),
            "has_video",
            "video_viewings",
            "is_shared",
            "is_studio",
            "is_live",
            "furnished",
            "unfurnished",
        )
    }
)


@settings(max_examples=200)
@given(flags=FLAGS, facts=FACTS, furnishing=st.sampled_from(("any", "furnished", "unfurnished")))
def test_requested_features_obey_three_valued_data_contract(flags, facts, furnishing):
    options = filters(location="London", radius_distance=2, furnishing=furnishing, **flags)
    item = listing(rent_pcm_pence=None, bedrooms=None, bathrooms=None, **facts)
    source = SearchData([])
    required_fields = {stored for requested, stored in FEATURES.items() if flags[requested]}
    if not flags["include_unavailable"]:
        required_fields.add("is_live")
    if flags["no_shared"]:
        required_fields.add("is_shared")
    if flags["no_studios"]:
        required_fields.add("is_studio")
    if furnishing != "any":
        required_fields.add(furnishing)
    video_positive = facts["has_video"] is True or facts["video_viewings"] is True
    if flags["video"] and not video_positive:
        required_fields.update(("has_video", "video_viewings"))
    missing = {field for field in required_fields if facts[field] is None}
    if missing:
        with pytest.raises(SearchError) as caught:
            matches_criteria(*options, item, source)
        assert any(field in str(caught.value) for field in missing)
    else:
        accepted = (
            all(facts[stored] is True for requested, stored in FEATURES.items() if flags[requested])
            and (flags["include_unavailable"] or facts["is_live"] is True)
            and (not flags["no_shared"] or facts["is_shared"] is False)
            and (not flags["no_studios"] or facts["is_studio"] is False)
            and (not flags["video"] or video_positive)
            and (furnishing == "any" or facts[furnishing] is True)
        )
        assert matches_criteria(*options, item, source) is accepted

    # Unknown facts, including numeric facts, are irrelevant when not requested.
    assert matches_criteria(
        *filters(location="London", radius_distance=2, include_unavailable=True), item, source
    )
