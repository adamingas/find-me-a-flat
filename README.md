# OpenRent → SQLite

A `uv`-managed async Python CLI built with [Click](https://click.palletsprojects.com/en/stable/) that searches OpenRent and archives listing metadata and full-size images in a local SQLite database. No account is required for the public sources used here.

Fetching uses `asyncio` and `httpx.AsyncClient`. Listing pages and image downloads run concurrently through bounded worker pools; `--concurrency` sets the maximum simultaneous HTTP requests (default **4**, range 1–32). The default `--delay 0.5` applies across all workers, including redirects, so increasing concurrency does not multiply the request rate. Server `Retry-After` cooldowns are shared too. SQLite operations run on one dedicated worker thread, keeping writes serialized without blocking network requests; HTML parsing and image verification also run off the event loop. Pending image results use bounded buffering rather than retaining every downloaded image in memory.

`--location` is required for every search; there is no preset location. Pass an area, address, station/landmark, or postcode that OpenRent can resolve. Quote values containing spaces. For the Victoria area use `--location "Victoria, London"`; to centre specifically on the station use `--location "Victoria Station, London"`. Both have been verified against OpenRent. The resolved point is the centre of the requested radius, rather than a neighbourhood boundary. Qualify ambiguous place names with the city, or use a full postcode for a more specific centre. Raw latitude/longitude input is not supported by this CLI.

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from this directory. `uv` installs the locked dependencies automatically.

```sh
uv run openrent fetch \
  --location "Victoria, London" \
  --radius-distance 2 \
  --rent-min 1200 --rent-max 2500 \
  --bedrooms-min 1 --bedrooms-max 2 \
  --no-shared \
  --db data/victoria.sqlite
```

Equivalent script entry point:

```sh
uv run fetch_openrent.py --location "Victoria, London" --radius-distance 2 --rent-max 2500
```

London travel-time search:

```sh
uv run openrent fetch \
  --location "Victoria Station, London" \
  --radius-minutes 25 \
  --rent-max 2500 --bedrooms-min 1 --bedrooms-max 2 \
  --pets --garden \
  --db data/victoria.sqlite
```

Choose exactly one radius mode. Distance defaults to **kilometres**; use `--distance-unit miles` for miles. Fractional distances are supported. Rent bounds are **monthly GBP**, inclusive, with up to two decimal places. Bedrooms and bathrooms bounds are inclusive; zero bedrooms means studios. As on OpenRent, shared rooms have an effective bedroom count of -1 for filtering, so any `--bedrooms-min 0` or higher excludes them. To search rooms, omit the bedroom minimum and use `--property-type room`.

OpenRent's travel-time search is [London-only and combines train, Tube, and walking](https://help.openrent.co.uk/hc/en-gb/articles/360002282471-What-is-the-commute-time-search). This script uses OpenRent's returned times, with no invented driving/public-transport modes. It detects and rejects OpenRent's silent kilometre fallback for unsupported locations.

## Filters and controls

`uv run openrent fetch --help` shows every option.

| Flags | Meaning |
| --- | --- |
| `--rent-min`, `--rent-max` | Monthly GBP bounds; aliases `--min-rent`, `--max-rent` |
| `--bedrooms-min`, `--bedrooms-max` | Bedroom bounds; aliases `--min-bedrooms`, `--max-bedrooms` |
| `--bathrooms-min`, `--bathrooms-max` | Bathroom bounds |
| `--property-type house/flat/room` | Repeat to include several types; studios count as flats |
| `--furnishing any/furnished/unfurnished` | Flexible listings offering either furnishing match both preferences |
| `--pets`, `--students`, `--professionals`, `--families` | Require the matching tenant preference |
| `--dss` | OpenRent's DSS/LHA covers rent or preferred flag |
| `--bills-included`, `--garden`, `--parking`, `--fireplace` | Require that feature |
| `--video` | Video tour or video viewings accepted |
| `--no-shared`, `--no-studios` | Exclude shared rooms or studios |
| `--move-in-before YYYY-MM-DD` | Available on or before that date |
| `--max-minimum-tenancy MONTHS` | Required minimum tenancy must be at most this many months |
| `--include-unavailable` | Include unavailable listings returned in the search data |
| `--sort distance/rent-asc/rent-desc/newest` | Local ordering; newest uses the first-listed timestamp |
| `--limit N` / `--max-properties N` | Import only the first N matches; default imports **all** matches |
| `--dry-run` | Print matching IDs, prices, distances/times, and links without creating a DB |
| `--skip-images` | Save image associations without bytes; the next normal run downloads them |
| `--no-source-html` | Omit the sanitized HTML backup while retaining extracted data |
| `--delay`, `--timeout`, `--retries` | Minimum request interval (default 0.5s), request timeout, transient retries |
| `--concurrency` | Maximum simultaneous HTTP requests (default 4, range 1–32); shared pacing still applies |
| `--cookie-file PATH` | Optional Netscape-format cookies exported from your own session |
| `--quiet` | Suppress progress output; retain the final report and errors |

Only available listings are imported by default. Missing data needed to evaluate a requested filter causes an explicit error, so a changed response cannot silently exclude listings. Explicit bedroom/bathroom maxima are exact, including 8 or more.

Check a search before downloading everything:

```sh
uv run openrent fetch --location "Victoria Station, London" --radius-minutes 15 --rent-max 2500 --dry-run
uv run openrent stats --db data/victoria.sqlite
uv run openrent schema
```

## Scheduled scanning

The foreground daemon accepts the same location, radius, rent, bedroom, image, and other scan flags as `fetch`. Supply your own quoted five-field cron expression:

```sh
uv run openrent daemon \
  --cron "*/15 * * * *" \
  --timezone Europe/London \
  --location "Victoria Station, London" \
  --radius-minutes 15 \
  --rent-max 2500 --bedrooms-min 1 --bedrooms-max 2 \
  --db data/victoria.sqlite \
  --export-csv data/victoria.csv
```

This example scans at minutes 0, 15, 30 and 45 of each hour. The five fields are **minute, hour, day-of-month, month, day-of-week**. Examples: `0 * * * *` hourly, `0 9,18 * * *` at 09:00 and 18:00, or `*/30 8-22 * * mon-fri` every half hour from 08:00 through 22:30 on weekdays. The default timezone is `Europe/London`; another IANA timezone can be supplied explicitly. There is no preset location, radius, rent, or cron schedule.

The daemon waits for the next matching time by default. Add `--run-now` for an immediate first scan. `--max-runs N` stops after N attempts, including failures and the immediate scan. Omit it to keep running. Preview the next five scheduled times without HTTP requests or database changes:

```sh
uv run openrent daemon \
  --cron "*/15 * * * *" --timezone Europe/London \
  --location "Victoria, London" --radius-distance 2 \
  --check-schedule
```

The daemon awaits scans and uses nonblocking async waits between scheduled times. Scans run sequentially, while requests within each scan run concurrently. Scheduled ticks that elapse during a scan are skipped, so a slow scan never creates a queue or overlaps the next one. Scan and export errors are logged and the daemon continues on the next tick. Ctrl+C or SIGTERM cancels the current scan, joins its request workers, closes clients and database handles, and preserves already committed properties/images. Already submitted SQLite writes and atomic CSV exports finish before the scan lock is released. A graceful daemon shutdown returns 0; a bounded daemon run returns 1 if any attempt failed. As before, repeated scans update the same listing IDs and reuse downloaded images.

Only one manual scan or daemon can hold a given database at a time. The persistent `DB.scan.lock` file uses an operating-system lock; it does not represent a stale active daemon after the process exits. The lock is released automatically on exit or process termination. Dry runs and schedule previews do not acquire it. CSV export remains available while the daemon runs through a read-only SQLite connection.

Cron supports ordinary numeric values, comma lists, ascending ranges, positive steps, and three-letter month/day names. Sunday is 0 or 7. When both day-of-month and day-of-week are restricted, either match triggers a scan. Six/seven-field cron, macros such as `@hourly`, and extensions such as `L`, `W`, `#`, `?`, `H`, and `R` are rejected. During London's spring clock change, nonexistent local scheduled times are skipped. During the autumn change, matching times in the repeated hour can run once in each occurrence.

Keep the process and host running to maintain scanning. The command runs in the foreground and does not install an OS cron entry, launch agent, system service, or Codex automation. A terminal session or your existing process supervisor can keep it alive; after restarting, it resumes at the next future tick rather than replaying historical scans.

## Selected-field CSV export

Export any supported field subset for Google Sheets, Excel, or another system that imports tables:

```sh
uv run openrent export \
  --db data/victoria.sqlite \
  --output data/victoria.csv \
  --columns id,url,title,postcode,rent_pcm,bedrooms,nearest_tube_station,nearest_tube_walk_minutes,nearest_rail_station,nearest_rail_walk_minutes

uv run openrent export --list-columns
```

The default subset includes 17 fields: ID, URL, title, displayed address, postcode, monthly rent, bedrooms, bathrooms, available date, coordinates, nearest Tube and rail names and walking minutes, first photo URL, and status. Rent aliases `rent_pcm`, `rent_weekly`, and `deposit` are exact pounds with two decimals; the original `*_pence` fields are also available. Station fields rank OpenRent's supplied nearby stations by walking minutes. Image fields contain source URLs/counts, with the original image bytes staying in SQLite.

Exports use a read-only database connection and atomically replace the CSV with one row per property, sorted by ID. They never append duplicate rows. Listing text that could be interpreted as a spreadsheet formula is escaped as literal text. Errors preserve the previous output file. Database files, SQLite sidecars, and scan lock files cannot be used as the CSV destination.

Without `--search-id`, export includes archived properties across searches. `--active-only` restricts it to listings currently disclosed as live. With `--search-id ID`, it selects that search's matches; `--active-only` then selects its active matches. The optional `distance_km`, `commute_minutes`, and `search_active` fields are supplied only for a selected search. List saved searches with:

```sh
sqlite3 -header -column data/victoria.sqlite \
  'SELECT id, location, last_search_complete, last_completed_at FROM searches;'
```

For the daemon, `--export-csv FILE` refreshes a CSV containing the current search's active matches after each successful full scan. `--export-columns id,url,rent_pcm,...` selects a subset. Failed scans/exports preserve the previous CSV. Automatic export rejects `--limit` and `--dry-run` because they cannot provide a fresh complete search snapshot. Manual export can still read a partially populated archive.

Google Sheets can import the CSV immediately using File → Import. Automatic API synchronization is feasible with a service account or OAuth credentials, a target spreadsheet, and property-ID-based row updates. [The integration assessment](docs/google-sheets.md) covers authentication, preserving manual Notes columns, retries, quotas, and image previews. An authenticated Sheets writer is a future extension; CSV export is implemented now, and this project does not upload to or modify any cloud spreadsheet.

## Stored data

[The schema](src/openrent/schema.sql) has real columns, typed scalar feature rows, foreign keys, checks, and indexes. Money is stored as integer **pence**; unknown values are SQL `NULL`. A property is an OpenRent advert, keyed by its listing ID.

| Table/view | Contents |
| --- | --- |
| `properties` | OpenRent ID and canonical URL; title, full text and HTML description; property type; displayed street/address, locality, postcode, coordinates; bedrooms, bathrooms, tenant limit; monthly/weekly rent and deposit; availability and tenancy; furnishing, amenities and tenant preferences; status, first-listed time, EPC; public landlord information; first/last seen and changed timestamps |
| `property_features` | Other queryable scalar facts grouped by section: feature label/key, typed text/integer/real/boolean value, and unit; includes summary fields, landlord verification/response stats and Street View metadata |
| `property_images` | Original image URL, order, kind, caption, dimensions, download status, content hash, HTTP validators and last download error |
| `image_blobs` | **Actual image bytes as SQLite BLOBs**, MIME type, byte count and SHA-256; one copy per unique content hash |
| `nearby_places` | Nearby transport and schools, walking times and any supplied distance/coordinates |
| `media_links` | Video/tour links; video bytes are not downloaded |
| `searches`, `search_filters` | Deduplicated search definitions and individual scalar filter parameters |
| `search_matches` | Deduplicated search/property links with distance or commute minutes and current-match status |
| `property_summary` | Convenient view with rent in pounds and image counts |

Gallery images and the listing's static map are downloaded; site logos and UI icons are excluded. Public sources may reveal approximate coordinates and a displayed street/postcode, rather than a full door-number address. Missing facts are retained as unknown rather than inferred from marketing descriptions. Where an advert exposes changing nested source schemas, `properties.extra_metadata_json` preserves only those irregular extras; the listing itself, images, filters, features and nearby places are normalized. A sanitized listing HTML backup preserves additional source material without login forms or request tokens.

Example queries with the standard SQLite CLI:

```sh
sqlite3 -header -column data/victoria.sqlite \
  'SELECT id, title, postcode, rent_pcm, bedrooms, latitude, longitude, downloaded_image_count FROM property_summary;'

sqlite3 -header -column data/victoria.sqlite \
  "SELECT property_id, name, kind, walking_minutes FROM nearby_places ORDER BY property_id, walking_minutes;"
```

To retrieve an image, join `property_images.content_sha256` to `image_blobs.sha256` and read `image_blobs.content`. This example exports a listing's downloaded images:

```sh
uv run python - <<'PY'
import sqlite3
from pathlib import Path

property_id = 1234567  # Replace with an ID stored in your database.
output = Path('exported-images') / str(property_id)
output.mkdir(parents=True, exist_ok=True)
with sqlite3.connect('data/victoria.sqlite') as db:
    rows = db.execute('''
        SELECT i.position, i.kind, b.content, b.content_type
        FROM property_images i JOIN image_blobs b ON b.sha256 = i.content_sha256
        WHERE i.property_id = ? ORDER BY i.position
    ''', (property_id,))
    suffixes = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
    for position, kind, content, mime in rows:
        suffix = suffixes.get(mime, '.img')
        (output / f'{position:03d}-{kind}{suffix}').write_bytes(content)
PY
```

## Repeat runs and failures

Run the same command again to refresh existing properties and discover new IDs. Identical imports do **not** append property, feature, image, search or match rows. First-seen timestamps stay fixed; last-seen timestamps refresh, and changed facts update in place. Existing downloaded image URLs are skipped. Matching image bytes across URLs/listings share a single BLOB. Removed gallery associations disappear from the current snapshot while archived image bytes remain in the database.

The importer commits each property and image incrementally. Failed image downloads are recorded and retried on the next execution. A failed or limited search does not deactivate earlier matches. After a successful full search, previous matches absent from that same search become inactive; their property records remain archived. Being absent from one search does not imply a listing is globally unavailable. Exit code 1 indicates an error, and 130 an interruption; successful saved work is retained.

## Sources and verification

The importer uses OpenRent's public website request flow:

1. `GET /search/search_bycommutetime` resolves the location and supplies all embedded candidate IDs, coordinates, times/distances, prices and filters.
2. `GET /search/propertiesbyid?ids=...&ids=...` supplies **JSON summaries**, in the website's batches of 20. If that endpoint fails, detail pages remain the fallback.
3. `GET /{listing_id}` redirects to the canonical detail page, supplying richer metadata and the original gallery image URLs.

These are the website's internal endpoints, **not a documented supported developer API**. Search filtering is repeated locally because the initial page contains a geographic superset, and OpenRent applies many filters in JavaScript. Integer server radii are widened slightly to include boundary values; exact requested distance is then enforced using the exposed coordinates and great-circle distance. The parser rejects missing essential arrays, missing or inconsistent reported totals, mismatched lengths and unexpected units rather than treating a broken source as an empty successful search. Location and commute resolution errors are explicit. Fetching a very large area can produce many listings and a large database.

By default every matching ID in the full embedded search arrays receives a detail request; the first 20 visible cards and the 20-ID summary batches do not limit the scan. All discovered gallery images are downloaded unless `--skip-images` is supplied. Failed detail/image requests leave the scan incomplete and return an error; `--limit` deliberately makes a partial scan. This establishes coverage of the returned search response. It does not independently establish coverage of OpenRent's entire inventory: server-side result caps, listings omitted from public search, and account-only content have not been ruled out. A server that truncates both the ID array and its reported total would not be detected by the count check. Gallery extraction uses the exposed markup; there is no independent source photo-count check, and videos are retained as links.

```sh
uv run pytest -q
uv run ruff check src tests
```

Five Hypothesis tests exercise our money conversion, local filters, search parser and database updates. The filter tests use a fixed generated candidate snapshot; they do not assert how OpenRent behaves across different live requests. Database tests check that repeated identical snapshots add no duplicate rows, while changed prices and descriptions update the existing listing. Focused regression tests cover sanitized real markup, async cancellation, HTTP retries, SQL constraints, cron timing and import recovery. Live checks also verified distance and London commute searches, full metadata/image downloads, and a second import adding zero properties and downloading zero images.
