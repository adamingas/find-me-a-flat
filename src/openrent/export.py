"""Read-only, repeatable tabular exports of the SQLite listing archive."""

from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from .db import PROPERTY_COLUMNS, review_path_for

DEFAULT_COLUMNS = (
    "id",
    "url",
    "title",
    "address_display",
    "postcode",
    "rent_pcm",
    "bedrooms",
    "bathrooms",
    "available_from",
    "latitude",
    "longitude",
    "nearest_tube_station",
    "nearest_tube_walk_minutes",
    "nearest_rail_station",
    "nearest_rail_walk_minutes",
    "image_url",
    "status",
)

# User-selected column names only ever select expressions from this whitelist.
_EXPRESSIONS = {
    "id": "p.id",
    **{
        name: f'p."{name}"'
        for name in PROPERTY_COLUMNS
        if name not in {"source_html", "description_html"}
    },
    "first_seen_at": "p.first_seen_at",
    "last_seen_at": "p.last_seen_at",
    "updated_at": "p.updated_at",
    "rent_pcm": "p.rent_pcm_pence",
    "rent_weekly": "p.rent_weekly_pence",
    "deposit": "p.deposit_pence",
    "image_url": (
        "(SELECT i.source_url FROM property_images i "
        "WHERE i.property_id = p.id AND i.kind = 'photo' "
        "ORDER BY i.position, i.source_url LIMIT 1)"
    ),
    "photo_count": (
        "(SELECT count(*) FROM property_images i WHERE i.property_id = p.id AND i.kind = 'photo')"
    ),
    "downloaded_photo_count": (
        "(SELECT count(*) FROM property_images i "
        "WHERE i.property_id = p.id AND i.kind = 'photo' "
        "AND i.download_status = 'downloaded')"
    ),
}


def _nearest(kind: str, column: str) -> str:
    # Both arguments are internal constants, never interpolated user input.
    return (
        f"(SELECT n.{column} FROM nearby_places n "
        f"WHERE n.property_id = p.id AND n.kind = '{kind}' "
        "ORDER BY n.walking_minutes IS NULL, n.walking_minutes, n.name LIMIT 1)"
    )


for _mode, _kind in (("tube", "underground"), ("rail", "national_rail")):
    _EXPRESSIONS[f"nearest_{_mode}_station"] = _nearest(_kind, "name")
    _EXPRESSIONS[f"nearest_{_mode}_walk_minutes"] = _nearest(_kind, "walking_minutes")

EXPORT_COLUMNS = tuple(dict.fromkeys((*DEFAULT_COLUMNS, *_EXPRESSIONS)))
_MONEY_COLUMNS = {"rent_pcm", "rent_weekly", "deposit"}


def available_columns() -> tuple[str, ...]:
    """Return selectable columns; the default compact columns appear first."""
    return EXPORT_COLUMNS


def _csv_value(value: Any, column: str) -> Any:
    if value is not None and column in _MONEY_COLUMNS:
        # Work directly with integer pence, avoiding binary floating point loss.
        pence = int(value)
        major, minor = divmod(abs(pence), 100)
        return f"{'-' if pence < 0 else ''}{major}.{minor:02d}"
    if isinstance(value, str):
        # A CSV has no cell types. Prevent listing text from being executed as
        # a spreadsheet formula, including prefixes hidden after whitespace.
        stripped = value.lstrip()
        if (value and ord(value[0]) < 32) or stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def validate_destination(db_path: Path, output: Path) -> None:
    """Reject destinations that could replace an archive or its working files."""
    db_path = Path(db_path).resolve()
    output = Path(output)
    resolved_output = output.resolve()
    review_path = review_path_for(db_path)
    protected = (
        *(
            Path(str(database) + suffix)
            for database in (db_path, review_path)
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
        Path(str(db_path) + ".scan.lock"),
        Path(str(db_path) + ".review.scan.lock"),
    )
    for path in protected:
        if resolved_output == path.resolve() or (
            output.exists() and path.exists() and os.path.samefile(output, path)
        ):
            raise ValueError(
                "CSV output must not be the SQLite database, its sidecars, or scan lock"
            )


def export_csv(
    db_path: Path,
    output: Path,
    columns: list[str] | None = None,
    active_only: bool = False,
) -> int:
    """Atomically replace a UTF-8 CSV with one row per property, sorted by ID.

    ``active_only`` selects listings disclosed as live. Reads the shared archive.

    Listing text with spreadsheet-formula prefixes receives a leading literal
    apostrophe. Image columns contain source URLs and counts, never image bytes.
    """
    selected = tuple(DEFAULT_COLUMNS if columns is None else columns)
    if not selected:
        raise ValueError("Select at least one export column")
    unknown = [name for name in selected if name not in _EXPRESSIONS]
    if unknown:
        raise ValueError(f"Unknown export column(s): {', '.join(unknown)}")
    if len(set(selected)) != len(selected):
        raise ValueError("Export columns must not contain duplicates")

    db_path = Path(db_path).resolve()
    output = Path(output)
    validate_destination(db_path, output)

    projection = ", ".join(f'{_EXPRESSIONS[name]} AS "{name}"' for name in selected)
    query = f"SELECT {projection} FROM properties p"
    if active_only:
        query += " WHERE p.is_live = 1"
    query += " ORDER BY p.id"

    temporary: Path | None = None
    try:
        # URI mode=ro also fails for missing files instead of creating an empty
        # SQLite archive. Do not use immutable mode: a daemon may have fresh WAL data.
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
            cursor = db.execute(query)
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(selected)
                count = 0
                for row in cursor:
                    writer.writerow(
                        [_csv_value(value, name) for name, value in zip(selected, row, strict=True)]
                    )
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
            temporary = None
            return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
