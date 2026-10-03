"""Repeated snapshots are stable; new facts refresh an existing listing by ID."""

import hashlib

from hypothesis import example, given, settings
from hypothesis import strategies as st

from openrent.db import Database
from openrent.models import Feature, Image, Property

scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**31), 2**31 - 1),
    st.floats(-1e12, 1e12, allow_nan=False, allow_infinity=False),
    st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=24),
)
updates = st.lists(
    st.tuples(
        st.integers(1, 6),
        st.integers(0, 100_000_000),
        st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=80),
        st.binary(min_size=1, max_size=32),
        scalar,
    ),
    min_size=1,
    max_size=20,
)


@settings(max_examples=50)
@example(
    records=[
        (1, 100, "Original description", b"same", True),
        (1, 200, "Original description", b"same", True),
        (1, 200, "Updated description: café near the tube", b"same", True),
        (1, 200, "Updated description: café near the tube", b"same", "Ask landlord"),
        (2, 300, "Another flat", b"same", None),
    ]
)
@given(records=updates)
def test_identical_snapshots_are_idempotent_and_changed_listings_update_existing_rows(records):
    expected = {
        property_id: (rent, description, content, value)
        for property_id, rent, description, content, value in records
    }
    all_contents = {content for _, _, _, content, _ in records}
    with Database(":memory:") as db:
        first_seen = {}
        current_snapshots = {}

        for property_id, rent, description, content, value in records:
            pictures = [
                Image(f"https://images.openrent.co.uk/{property_id}/{position}.jpg", position)
                for position in range(2)
            ]
            prop = Property(
                property_id,
                f"https://www.openrent.co.uk/{property_id}",
                rent_pcm_pence=rent,
                description=description,
                detail_complete=True,
                features=[Feature("variable", "Variable fact", value=value)],
                images=pictures,
            )
            inserted = db.upsert_property(prop)
            assert inserted is (property_id not in first_seen)
            row = db.get_property(property_id)
            assert row["rent_pcm_pence"] == rent
            assert row["description"] == description
            first_seen.setdefault(property_id, row["first_seen_at"])
            assert row["first_seen_at"] == first_seen[property_id]
            assert db.counts()["properties"] == len(first_seen)

            # Each generated update must replace the previous scalar value,
            # including clearing the old typed column when its type changes.
            feature = db.connection.execute(
                "SELECT * FROM property_features WHERE property_id = ?", (property_id,)
            ).fetchone()
            kind = {str: "text", int: "integer", float: "real", bool: "boolean", type(None): None}[
                type(value)
            ]
            assert feature["value_type"] == kind
            assert sum(
                feature[f"value_{name}"] is not None
                for name in ("text", "integer", "real", "boolean")
            ) == (value is not None)
            if kind:
                assert feature[f"value_{kind}"] == value

            for picture in pictures:
                db.store_image(property_id, picture, content, "image/jpeg")
            current_snapshots[property_id] = (prop, content)

        first_counts = db.counts()
        current_rows = {
            row["id"]: {key: value for key, value in dict(row).items() if key != "last_seen_at"}
            for row in db.connection.execute("SELECT * FROM properties")
        }

        # Replay the current snapshot, rather than an old sequence of different
        # prices. Only the observation timestamp may refresh on unchanged data.
        for property_id, (prop, content) in current_snapshots.items():
            assert db.upsert_property(prop) is False
            row = db.get_property(property_id)
            assert {
                key: value for key, value in dict(row).items() if key != "last_seen_at"
            } == current_rows[property_id]
            for picture in prop.images:
                assert db.store_image(property_id, picture, content, "image/jpeg") is False

        assert db.counts() == first_counts
        assert first_counts["properties"] == len(expected)
        assert first_counts["property_features"] == len(expected)
        assert first_counts["downloaded_images"] == 2 * len(expected)
        assert first_counts["image_blobs"] == len(all_contents)

        for property_id, (rent, description, content, _) in expected.items():
            row = db.get_property(property_id)
            assert row["rent_pcm_pence"] == rent
            assert row["description"] == description
            assert row["first_seen_at"] == first_seen[property_id]
            photos = db.connection.execute(
                "SELECT b.content FROM property_images i JOIN image_blobs b "
                "ON b.sha256 = i.content_sha256 WHERE i.property_id = ?",
                (property_id,),
            ).fetchall()
            assert [photo[0] for photo in photos] == [content, content]

        blobs = db.connection.execute(
            "SELECT sha256, content, byte_length FROM image_blobs"
        ).fetchall()
        assert {row["content"] for row in blobs} == all_contents
        assert all(
            row["sha256"] == hashlib.sha256(row["content"]).hexdigest()
            and row["byte_length"] == len(row["content"])
            for row in blobs
        )
        assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
