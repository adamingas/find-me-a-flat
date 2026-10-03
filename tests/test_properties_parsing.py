"""Generated public-search records retain all listings or reject corrupt responses."""

import json
from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openrent.parsing import ParseError, parse_search


@st.composite
def search_records(draw):
    size = draw(st.integers(min_value=0, max_value=40))
    return draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "id": st.integers(min_value=1, max_value=20_000_000),
                    "rent_pence": st.integers(min_value=0, max_value=10_000_000),
                    "bedrooms": st.integers(min_value=0, max_value=20),
                    "bathrooms": st.integers(min_value=0, max_value=20),
                    "live": st.booleans(),
                    "studio": st.booleans(),
                    "shared": st.booleans(),
                    "pets": st.booleans(),
                    "latitude": st.integers(min_value=-90_000_000, max_value=90_000_000),
                    "longitude": st.integers(min_value=-180_000_000, max_value=180_000_000),
                    "available_days": st.integers(min_value=-365, max_value=365),
                    "minimum_tenancy": st.integers(min_value=1, max_value=36),
                    "travel": st.integers(min_value=0, max_value=30_000),
                }
            ),
            min_size=size,
            max_size=size,
            unique_by=lambda record: record["id"],
        )
    )


def search_html(assignments):
    return (
        "<script>"
        + "".join(f"var {name} = {json.dumps(value)};" for name, value in assignments.items())
        + "</script>"
    )


@given(
    records=search_records(),
    unit=st.sampled_from(["km", "miles", "minutes"]),
    reference_date=st.dates(min_value=date(2020, 1, 1), max_value=date(2040, 12, 31)),
    corrupt_array=st.sampled_from(
        [
            "prices",
            "bedrooms",
            "bathrooms",
            "islivelistBool",
            "isstudio",
            "isshared",
            "PROPERTYLISTLATITUDES",
            "PROPERTYLISTLONGITUDES",
            "pets",
            "availableFrom",
            "minimumTenancy",
            "PROPERTYLISTCOMMUTEORDISTANCE",
        ]
    ),
    missing_essential=st.sampled_from(
        [
            "prices",
            "bedrooms",
            "bathrooms",
            "islivelistBool",
            "isstudio",
            "isshared",
            "PROPERTYLISTLATITUDES",
            "PROPERTYLISTLONGITUDES",
        ]
    ),
    count_delta=st.integers(min_value=1, max_value=100),
    malformed_total=st.one_of(
        st.text(alphabet="abcdef", min_size=1, max_size=12),
        st.floats(min_value=0.1, max_value=0.9),
    ),
)
def test_generated_search_is_exhaustive_and_rejects_inconsistent_source(
    records, unit, reference_date, corrupt_array, missing_essential, count_delta, malformed_total
):
    assignments = {
        "PROPERTYIDS": [row["id"] for row in records],
        "NUMBEROFPROPERTIES": len(records),
        "prices": [
            f"£{row['rent_pence'] // 100:,}.{row['rent_pence'] % 100:02d}" for row in records
        ],
        "bedrooms": [row["bedrooms"] for row in records],
        "bathrooms": [row["bathrooms"] for row in records],
        "islivelistBool": [row["live"] for row in records],
        "isstudio": [row["studio"] for row in records],
        "isshared": [row["shared"] for row in records],
        "pets": [row["pets"] for row in records],
        "PROPERTYLISTLATITUDES": [row["latitude"] / 1_000_000 for row in records],
        "PROPERTYLISTLONGITUDES": [row["longitude"] / 1_000_000 for row in records],
        "availableFrom": [row["available_days"] for row in records],
        "minimumTenancy": [row["minimum_tenancy"] for row in records],
        "PROPERTYLISTCOMMUTEORDISTANCE": [row["travel"] / 1000 for row in records],
        "PROPERTYLISTCOMMORDISTANCEUNIT": unit,
    }
    result = parse_search(search_html(assignments), reference_date=reference_date)
    assert result.total == len(records)
    assert [candidate.property.id for candidate in result.candidates] == [
        row["id"] for row in records
    ]
    for row, candidate in zip(records, result.candidates, strict=True):
        prop = candidate.property
        assert prop.rent_pcm_pence == row["rent_pence"]
        assert prop.bedrooms == row["bedrooms"]
        assert prop.bathrooms == row["bathrooms"]
        assert prop.is_live is row["live"]
        assert prop.is_studio is row["studio"]
        assert prop.is_shared is row["shared"]
        assert prop.pets_allowed is row["pets"]
        assert prop.latitude == row["latitude"] / 1_000_000
        assert prop.longitude == row["longitude"] / 1_000_000
        assert (
            prop.available_from
            == (reference_date + timedelta(days=row["available_days"])).isoformat()
        )
        assert prop.minimum_tenancy_months == row["minimum_tenancy"]
        travel = row["travel"] / 1000
        if unit == "minutes":
            assert candidate.commute_minutes == travel
            assert candidate.distance_km is None
        else:
            assert candidate.distance_km == pytest.approx(
                travel * (1.609344 if unit == "miles" else 1)
            )
            assert candidate.commute_minutes is None

    mismatched = dict(assignments)
    mismatched[corrupt_array] = [*assignments[corrupt_array], None]
    with pytest.raises(ParseError, match="not aligned"):
        parse_search(search_html(mismatched), reference_date=reference_date)
    for wrong_count in (len(records) + count_delta, malformed_total):
        with pytest.raises(ParseError):
            parse_search(search_html({**assignments, "NUMBEROFPROPERTIES": wrong_count}))
    for absent in ("NUMBEROFPROPERTIES", "PROPERTYIDS"):
        with pytest.raises(ParseError, match="Missing"):
            parse_search(
                search_html({key: value for key, value in assignments.items() if key != absent})
            )
    if records:
        with pytest.raises(ParseError, match="Missing essential"):
            parse_search(
                search_html(
                    {key: value for key, value in assignments.items() if key != missing_essential}
                )
            )
        duplicate = {
            key: [*value, value[0]] if isinstance(value, list) else value
            for key, value in assignments.items()
        }
        duplicate["NUMBEROFPROPERTIES"] += 1
        with pytest.raises(ParseError, match="duplicate property ID"):
            parse_search(search_html(duplicate))
