"""Archive fixtures and assertions about persisted output."""

from dataclasses import replace

from pipeline_worker import judgement

from openrent import db as database
from openrent.gyms import Gym, GymAmenities
from openrent.models import Feature, Image, MediaLink, NearbyPlace, Property
from openrent.review import Assessment, ReviewConfig

CONFIG = ReviewConfig("Bright flat", "a" * 64, "offline-model", 10)


def gym_payload(*walking_seconds):
    return GymAmenities(
        gyms=[
            Gym(
                osm_id=f"node/{index}",
                name=f"Gym {index}",
                latitude=51.441,
                longitude=-0.155,
                classification="leisure=fitness_centre",
                tags={"leisure": "fitness_centre"},
                walking_seconds=seconds,
            )
            for index, seconds in enumerate(walking_seconds, 1)
        ],
        origin_latitude=51.440875,
        origin_longitude=-0.154623,
        search_radius_m=1500,
        walking_speed_kmh=5,
    ).model_dump(mode="json")


async def table_counts(db):
    return {
        table: (await database.fetch_one(db, f"SELECT count(*) FROM {table}"))[0]
        for table in (
            "properties",
            "property_features",
            "nearby_places",
            "media_links",
            "property_images",
            "image_blobs",
        )
    }


async def image_content(db, property_id, source_url):
    row = await database.fetch_one(
        db,
        "SELECT b.content FROM property_images i JOIN image_blobs b "
        "ON b.sha256 = i.content_sha256 WHERE i.property_id = ? AND i.source_url = ?",
        (property_id, source_url),
    )
    return row[0] if row is not None else None


def listing(property_id=1):
    return Property(
        id=property_id,
        url=f"https://www.openrent.co.uk/property-to-rent/london/{property_id}",
        title="A bright flat",
        description="Two large windows",
        address_display="Example Road, London",
        rent_pcm_pence=200000,
        bedrooms=1,
        is_live=True,
        features=[Feature("floor", "Floor", value=2)],
        nearby_places=[NearbyPlace("Victoria", walking_minutes=8)],
        media_links=[MediaLink("https://example.com/video")],
        images=[Image("https://images.openrent.co.uk/living.jpg", caption="Living room")],
        extra_metadata={"irregular": {"size": 44}},
        detail_complete=True,
    )


async def store_listing(db, property):
    await database.upsert_property(db, property)
    for image in property.images:
        await database.store_image(db, property.id, image, b"original image", "image/jpeg")


def assessment(snapshot, decision="pass"):
    outcome = {"pass": True, "reject": False, "uncertain": None}[decision]
    result = judgement(outcome)
    return Assessment(
        snapshot.property_id,
        snapshot.fingerprint,
        snapshot.data,
        tuple(snapshot.data["images"]),
        result.model_dump_json(),
    )


async def add_listing(path, property_id, **changes):
    async with database.connect(path) as db:
        await store_listing(db, replace(listing(property_id), **changes))


async def saved_review(path, property_id, decision="pass"):
    await add_listing(path, property_id)
    async with database.connect(path) as db:
        return await database.save_review(
            db, CONFIG, assessment(await database.get_evidence(db, property_id), decision)
        )
