"""Additional rules over downloaded metadata. Edit here to add rules."""

from dbos import DBOS

from . import db as database
from .models import Property
from .runtime import resources


def matches_post_filter(property: Property) -> bool:
    """Require EPC A/B/C and an Underground walk of at most 11 minutes."""
    return (property.epc_rating or "").strip().upper() in {"A", "B", "C"} and any(
        place.kind == "underground"
        and place.walking_minutes is not None
        and place.walking_minutes <= 11
        for place in property.nearby_places
    )


@DBOS.step()
async def post_amenities_filter(property_id: int) -> bool:
    """Require a gym below thirteen minutes and reject known commutes over thirty."""
    rows = await database.query(
        resources().db,
        "SELECT COALESCE(MIN(json_extract(g.value, '$.walking_seconds')) < 13 * 60, 0) "
        "AND NOT EXISTS (SELECT 1 FROM amenities c "
        "WHERE c.property_id = ? AND c.type = 'commute' "
        "AND json_extract(c.data, '$.duration_minutes') > 30) AS passes "
        "FROM amenities a CROSS JOIN json_each(a.data, '$.gyms') g "
        "WHERE a.property_id = ? AND a.type = 'gym'",
        (property_id, property_id),
    )
    return bool(rows[0]["passes"])
