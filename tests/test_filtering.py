"""Business rules apply to downloaded facts before images are requested."""

from copy import deepcopy
from dataclasses import replace

import pytest
from archive_helpers import listing

from openrent.filtering import matches_post_filter
from openrent.models import NearbyPlace


@pytest.mark.parametrize(
    "epc, accepted",
    [
        ("A", True),
        ("B", True),
        (" c ", True),
        ("D", False),
        ("G", False),
        ("N/A", False),
        ("", False),
        (None, False),
    ],
)
def test_epc_requires_a_b_or_c(epc, accepted):
    prop = replace(
        listing(),
        epc_rating=epc,
        nearby_places=[NearbyPlace("Victoria", "underground", walking_minutes=11)],
    )
    original = deepcopy(prop)
    assert matches_post_filter(prop) is accepted
    assert prop == original


@pytest.mark.parametrize(
    "kind, minutes, accepted",
    [
        ("underground", 11, True),
        ("underground", 12, False),
        ("underground", 0, True),
        ("underground", None, False),
        ("rail", 5, False),
        ("overground", 5, False),
        (None, 5, False),
    ],
)
def test_walk_requires_a_known_underground_station_within_eleven_minutes(kind, minutes, accepted):
    prop = replace(
        listing(),
        epc_rating="B",
        nearby_places=[NearbyPlace("Station", kind, walking_minutes=minutes)],
    )
    assert matches_post_filter(prop) is accepted


def test_any_qualifying_station_is_enough_but_missing_stations_are_rejected():
    prop = replace(listing(), epc_rating="C", nearby_places=[])
    assert not matches_post_filter(prop)
    prop.nearby_places = [
        NearbyPlace("Rail", "rail", walking_minutes=1),
        NearbyPlace("Far Tube", "underground", walking_minutes=20),
        NearbyPlace("Near Tube", "underground", walking_minutes=10),
    ]
    assert matches_post_filter(prop)
