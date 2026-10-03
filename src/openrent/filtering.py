"""Hard-coded post-fetch rules over the saved archive. Edit this function to add rules."""

import sqlite3
from itertools import batched


def filter_property_ids(connection: sqlite3.Connection, property_ids: list[int]) -> list[int]:
    """Return input IDs with EPC A/B/C and a Tube walk of at most 11 minutes.

    The connection exposes all stored columns and related tables. This function
    only reads them; the database layer deletes rejected IDs after it returns.
    Missing walking times or EPC ratings do not satisfy the rules. EPC letters
    are compared without surrounding whitespace and regardless of case.
    """
    property_ids = list(dict.fromkeys(property_ids))
    passing = set()
    for batch in batched(property_ids, 500):
        placeholders = ",".join("?" for _ in batch)
        passing.update(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT p.id FROM properties p "
                "JOIN nearby_places n ON n.property_id = p.id "
                f"WHERE p.id IN ({placeholders}) "
                "AND UPPER(TRIM(p.epc_rating)) IN ('A', 'B', 'C') "
                "AND n.kind = 'underground' AND n.walking_minutes <= 11",
                batch,
            )
        )
    return [property_id for property_id in property_ids if property_id in passing]
