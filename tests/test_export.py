import csv
import sqlite3

import pytest

from openrent.db import Database
from openrent.export import export_csv
from openrent.models import Image, NearbyPlace, Property


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.sqlite"
    live = Property(
        id=10,
        url="https://www.openrent.co.uk/property-to-rent/london/10",
        title="1 Bed Flat, Café Road",
        address_display="12 Café Road, London",
        postcode="SW1V 1AA",
        bedrooms=1,
        bathrooms=1,
        rent_pcm_pence=185099,
        rent_weekly_pence=42701,
        deposit_pence=213461,
        latitude=51.50012345,
        longitude=-0.12345678,
        is_live=True,
        status="Available",
        pets_allowed=False,
        furnishing="Furnished",
        images=[
            Image("https://images.openrent.co.uk/map.png", position=0, kind="map"),
            Image("https://images.openrent.co.uk/z.jpg", position=1),
            Image("https://images.openrent.co.uk/a.jpg", position=1),
            Image("https://images.openrent.co.uk/last.jpg", position=2),
        ],
        nearby_places=[
            NearbyPlace("Unknown Tube", "underground", distance_km=0.01),
            NearbyPlace("Zeta Tube", "underground", walking_minutes=5),
            NearbyPlace("Alpha Tube", "underground", walking_minutes=5),
            NearbyPlace("Rail", "national_rail", walking_minutes=2),
            NearbyPlace("Overground", "overground", walking_minutes=1),
            NearbyPlace("School", "primary_school", walking_minutes=0),
        ],
    )
    unknown = Property(
        id=20,
        url="https://www.openrent.co.uk/property-to-rent/london/20",
        title="Unknown availability",
        nearby_places=[NearbyPlace("Tube Without Estimate", "underground")],
    )
    withdrawn = Property(
        id=30,
        url="https://www.openrent.co.uk/property-to-rent/london/30",
        title="Withdrawn listing",
        is_live=False,
        status="Let",
    )
    with Database(path) as db:
        # Insert out of ID order and include multiple station/image associations.
        for prop in (withdrawn, live, unknown):
            db.upsert_property(prop)
        db.store_image(10, live.images[2], b"photo contents", "image/jpeg")
        db.store_image(10, live.images[0], b"map contents", "image/png")
    return path


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_station_modes_known_walking_estimates_ties_and_missing_values(archive, tmp_path):
    output = tmp_path / "stations.csv"
    export_csv(archive, output)
    rows = read_csv(output)
    assert rows[0]["nearest_tube_station"] == "Alpha Tube"
    assert rows[0]["nearest_tube_walk_minutes"] == "5"
    assert rows[0]["nearest_rail_station"] == "Rail"
    assert rows[0]["nearest_rail_walk_minutes"] == "2"
    assert rows[1]["nearest_tube_station"] == "Tube Without Estimate"
    assert rows[1]["nearest_tube_walk_minutes"] == ""
    assert rows[1]["nearest_rail_station"] == ""
    assert rows[2]["nearest_tube_station"] == ""


def test_image_export_uses_first_photo_url_and_photo_counts(archive, tmp_path):
    output = tmp_path / "images.csv"
    export_csv(archive, output, ["id", "image_url", "photo_count", "downloaded_photo_count"])
    rows = read_csv(output)
    assert rows[0] == {
        "id": "10",
        "image_url": "https://images.openrent.co.uk/a.jpg",
        "photo_count": "3",
        "downloaded_photo_count": "1",
    }
    assert rows[1]["image_url"] == ""
    assert rows[1]["photo_count"] == "0"


def test_export_reads_pending_wal_data_without_modifying_it(tmp_path):
    path = tmp_path / "active.sqlite"
    with Database(path) as db:
        db.upsert_property(Property(id=1, url="https://example.test/1"))
        before = tuple(db.connection.iterdump())
        export_csv(path, tmp_path / "wal.csv", ["id"])
        assert read_csv(tmp_path / "wal.csv") == [{"id": "1"}]
        assert tuple(db.connection.iterdump()) == before


def test_missing_database_is_not_created_and_preserves_output(tmp_path):
    db_path = tmp_path / "missing.sqlite"
    output = tmp_path / "out.csv"
    output.write_text("previous result", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError):
        export_csv(db_path, output)
    assert not db_path.exists()
    assert output.read_text(encoding="utf-8") == "previous result"


def test_global_active_filter_uses_listing_availability(archive, tmp_path):
    output = tmp_path / "active.csv"
    assert export_csv(archive, output, ["id", "is_live"], active_only=True) == 1
    assert read_csv(output) == [{"id": "10", "is_live": "1"}]


@pytest.mark.parametrize("columns", [["source_html"]])
def test_invalid_columns_fail_before_replacing_existing_output(archive, tmp_path, columns):
    output = tmp_path / "out.csv"
    output.write_bytes(b"keep me")
    with pytest.raises(ValueError):
        export_csv(archive, output, columns)
    assert output.read_bytes() == b"keep me"


def test_write_failure_preserves_existing_output_and_cleans_temporary_file(
    archive, tmp_path, monkeypatch
):
    output = tmp_path / "out.csv"
    output.write_bytes(b"keep me")

    class FailingWriter:
        calls = 0

        def writerow(self, row):
            self.calls += 1
            if self.calls > 2:
                raise OSError("Disk write failure")

    monkeypatch.setattr("openrent.export.csv.writer", lambda stream: FailingWriter())
    with pytest.raises(OSError, match="Disk write failure"):
        export_csv(archive, output)
    assert output.read_bytes() == b"keep me"
    assert not list(tmp_path.glob(".out.csv.*.tmp"))


def test_replace_failure_preserves_existing_output_and_cleans_temporary_file(
    archive, tmp_path, monkeypatch
):
    output = tmp_path / "out.csv"
    output.write_bytes(b"keep me")

    def fail_replace(source, destination):
        raise OSError("Cannot replace result")

    monkeypatch.setattr("openrent.export.os.replace", fail_replace)
    with pytest.raises(OSError, match="Cannot replace result"):
        export_csv(archive, output)
    assert output.read_bytes() == b"keep me"
    assert not list(tmp_path.glob(".out.csv.*.tmp"))


@pytest.mark.parametrize("alias", ["direct", "hardlink"])
def test_database_cannot_be_used_as_csv_output(archive, tmp_path, alias):
    db_path = archive
    before = db_path.read_bytes()
    output = db_path
    if alias == "hardlink":
        output = tmp_path / "database-alias.csv"
        output.hardlink_to(db_path)
    with pytest.raises(ValueError, match="must not be the SQLite database"):
        export_csv(db_path, output)
    assert db_path.read_bytes() == before


@pytest.mark.parametrize(
    "suffix, alias",
    [(".scan.lock", "direct"), ("-shm", "symlink"), (".review.sqlite", "hardlink")],
)
def test_database_sidecars_and_scan_lock_cannot_be_overwritten(archive, tmp_path, suffix, alias):
    db_path = archive
    protected = db_path.with_name(db_path.name + suffix)
    content = b"existing sqlite sidecar or held scan lock"
    protected.write_bytes(content)
    output = protected
    if alias == "symlink":
        output = tmp_path / "sidecar-alias.csv"
        output.symlink_to(protected)
    elif alias == "hardlink":
        output = tmp_path / "review-alias.csv"
        output.hardlink_to(protected)
    with pytest.raises(ValueError, match="its sidecars, or scan lock"):
        export_csv(db_path, output)
    assert protected.read_bytes() == content
    assert output.read_bytes() == content


@pytest.mark.parametrize("suffix", ["-wal", ".review.sqlite-wal", ".review.scan.lock"])
def test_database_sidecar_paths_are_rejected_before_they_exist(archive, suffix):
    db_path = archive
    output = db_path.with_name(db_path.name + suffix)
    assert not output.exists()
    with pytest.raises(ValueError, match="its sidecars, or scan lock"):
        export_csv(db_path, output)
    assert not output.exists()
