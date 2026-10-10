# CLI and storage reference

For installation and the two main commands, see the [README](../README.md).
For model reviews and email delivery, see [review documentation](reviews.md).

## Locations and fetching

Fetching uses `asyncio` and `httpx.AsyncClient`. Listing pages and image downloads use `asyncio.gather` with a semaphore; `--concurrency` sets the maximum simultaneous HTTP requests (default **1**, range 1–32). After the search response, all summary groups and listings are scheduled with `gather`; detail fetches and image downloads share a semaphore, without sequential batch barriers. Requests start when semaphore capacity and the rate limit allow them. `--requests-per-second` (alias `--rps`) caps the shared start rate, defaults to 0.2 (one request every five seconds), and includes images, retries and redirect hops without bursts. Failed requests retry independently through one async retry decorator, with up to six attempts. `Retry-After` sets the wait when supplied; otherwise 429s wait 30, 60, 120, 240, then 300 seconds, and network/5xx errors use shorter exponential backoff. SQLite operations run on one dedicated worker thread, keeping writes serialized without blocking network requests; HTML parsing and image verification also run off the event loop. Each HTTP attempt holds the shared semaphore; backoff sleeps release its capacity. Image tasks save their bytes and return without retaining them.

`--location` is required for every search; there is no preset location. Pass an area, address, station/landmark, or postcode that OpenRent can resolve. Quote values containing spaces. For the Victoria area use `--location "Victoria, London"`; to centre specifically on the station use `--location "Victoria Station, London"`. Both have been verified against OpenRent. The resolved point is the centre of the requested radius, rather than a neighbourhood boundary. Qualify ambiguous place names with the city, or use a full postcode for a more specific centre. Raw latitude/longitude input is not supported by this CLI.

## More search examples

Distance-radius search:

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
| `--today` | Only listings first listed today, using the Europe/London calendar |
| `--max-minimum-tenancy MONTHS` | Required minimum tenancy must be at most this many months |
| `--include-unavailable` | Include unavailable listings returned in the search data |
| `--sort distance/rent-asc/rent-desc/newest` | Local ordering; newest uses the first-listed timestamp |
| `--dry-run` | Print matching IDs, prices, distances/times, and links without creating a DB |
| `--skip-images` | Save image associations without bytes; the next normal run downloads them |
| `--filter` / `--no-filter` | Enable/disable destructive post-fetch filtering; disabled by default |
| `--no-source-html` | Omit the sanitized HTML backup while retaining extracted data |
| `--timeout` | Request timeout; transient retries are automatic |
| `--concurrency` | Maximum simultaneous HTTP requests (default 1, range 1–32) |
| `--requests-per-second`, `--rps` | Shared HTTP start rate, including images, retries and redirects (default 0.2: one request every five seconds) |
| `--cookie-file PATH` | Optional Netscape-format cookies exported from your own session |
| `--quiet` | Suppress progress output; retain the final report and errors |

Only available listings are imported by default. Missing data needed to evaluate a requested filter causes an explicit error, so a changed response cannot silently exclude listings. Explicit bedroom/bathroom maxima are exact, including 8 or more.

[search.py](../src/openrent/search.py) contains two models: `ApiFilters` for location and distance/commute radius, and `WebsiteFilters` for rent, bedrooms, features, and other locally checked choices. Every model field defaults to `None` (no constraint). The CLI supplies location, one radius, kilometres as the default unit, and live-only listings by default. Only geographic parameters go to OpenRent; website filters are checked against returned metadata before fetching details and checked again before saving. All scans feed one shared listing archive, keyed only by OpenRent ID. The destructive post-fetch rules below remain a separate stage.

Check a search before downloading everything:

```sh
uv run openrent fetch --location "Victoria Station, London" --radius-minutes 15 --rent-max 2500 --dry-run
uv run openrent stats --db data/victoria.sqlite
uv run openrent schema
```

## Post-fetch deletion filter

Add `--filter` to `fetch` or `daemon` to keep only listings meeting **both** conditions: a recorded Tube (`underground`) station walking time of **11 minutes or less**, and an **EPC rating of A, B or C**. Exactly 11 minutes passes. Rail/Overground stations do not satisfy the walking rule. EPC letters are matched without surrounding whitespace and regardless of case. Listings with missing/unrecognised EPC ratings, ratings D–G, or no qualifying recorded Tube walking time are deleted.

```sh
uv run openrent fetch \
  --location "Victoria Station, London" --radius-minutes 25 \
  --rent-max 2500 --bedrooms-min 1 \
  --db data/victoria.sqlite --filter
```

The Python function `filter_property_ids(connection, property_ids)` in [filtering.py](../src/openrent/filtering.py) reads saved metadata and returns the passing IDs. Edit that function to add hard-coded rules; its SQLite connection exposes every stored column and related table. It runs on the database worker after detail metadata is saved, before downloading images or scheduled model review. It only examines successfully imported IDs from the current scan; unrelated archived listings are left alone.

Rejected listings are physically deleted, with cascading deletion of features, nearby places, media links, image associations and reviews. Their image BLOBs are removed only when no remaining listing or review references them. Rule evaluation and deletion use one transaction, so a filter or cleanup failure rolls back the pruning. Review profiles remain. The final scan report includes the number deleted.

`--no-filter` is the default and preserves the ordinary archive behaviour. Deleted IDs remain completed in the download queue; use `--refresh` to fetch and reevaluate them. Cached image bytes can be reused. `--filter --dry-run` is rejected because this rule requires saved detail metadata. Schedule previews with `--filter --check-schedule` remain read-only.

## Scheduled scanning

The foreground daemon accepts the same location, radius, rent, bedroom, image, and other scan flags as `fetch`. Supply your own quoted five-field cron expression:

```sh
uv run openrent daemon \
  --cron "*/15 * * * *" \
  --timezone Europe/London \
  --location "Victoria Station, London" \
  --radius-minutes 15 \
  --rent-max 2500 --bedrooms-min 1 --bedrooms-max 2 \
  --db data/victoria.sqlite
```

This example scans at minutes 0, 15, 30 and 45 of each hour. The five fields are **minute, hour, day-of-month, month, day-of-week**. Examples: `0 * * * *` hourly, `0 9,18 * * *` at 09:00 and 18:00, or `*/30 8-22 * * mon-fri` every half hour from 08:00 through 22:30 on weekdays. The default timezone is `Europe/London`; another IANA timezone can be supplied explicitly. There is no preset location, radius, rent, or cron schedule.

Discovery uses `--cron`; review uses `--review-cron`, defaulting to the discovery schedule when `--criteria-file` is supplied. Both run independently of the downloader. Discovery and new download tasks do not wait for older galleries to finish; HTTP requests still wait for the shared semaphore and rate limit. Discovery ticks are skipped only when discovery itself takes longer than its schedule; review likewise runs one cycle at a time.

The downloader immediately resumes persisted work on startup, even without `--run-now`. That flag additionally triggers immediate discovery and review. New discoveries wake the downloader; `--retry-interval` (default 60 seconds) checks unfinished jobs between discoveries. Per-request retries still respect `Retry-After` and exponential backoff. OpenRent search, summary, detail and image requests all use **one shared client, semaphore and rate limiter**. Review only uses complete stored galleries and never fetches images itself.

`--max-runs N` stops after N discovery attempts, waits for active downloads, makes a final download/review pass, and leaves failed jobs saved for the next start. Omit it for a persistent service. Ctrl+C or SIGTERM cancels and joins all tasks before closing HTTP and SQLite resources; submitted SQLite writes finish first. A graceful signal stop returns 0. A bounded run returns 1 if any discovery, download or review pass failed.

Preview the next five discovery times without HTTP requests or database changes:

```sh
uv run openrent daemon \
  --cron "0 * * * *" --timezone Europe/London \
  --location "Victoria, London" --radius-distance 2 \
  --check-schedule
```

The queue and successful stage results live in `<archive>.downloads.sqlite`; preserve it alongside the archive and review sidecar. Fresh search HTML is requested on each discovery, but saved summaries, detail metadata and image bytes are reused unless `--refresh` explicitly requests metadata again. A crash after an HTTP response but before its checkpoint commits can repeat that request. Already saved image bytes are reused. Filtered-out IDs stay completed and are not resurrected by ordinary discovery; `--refresh` permits reevaluating them.

Multiple manual scans can still share the archive. Per-property locks prevent duplicate concurrent downloads, and SQLite serializes writes. Separate processes have separate HTTP rate limits; run one daemon service when you want one global request budget. Business tables contain no search IDs or search histories; the download sidecar keeps the inputs needed to resume pending work.

Cron supports ordinary numeric values, comma lists, ascending ranges, positive steps, and three-letter month/day names. Sunday is 0 or 7. When both day-of-month and day-of-week are restricted, either match triggers a scan. Six/seven-field cron, macros such as `@hourly`, and extensions such as `L`, `W`, `#`, `?`, `H`, and `R` are rejected. During London's spring clock change, nonexistent local scheduled times are skipped. During the autumn change, matching times in the repeated hour can run once in each occurrence.

Keep the process and host running to maintain scanning. The command runs in the foreground and does not install an OS cron entry, launch agent, system service, or Codex automation. A terminal session or your existing process supervisor can keep it alive; after restarting, it immediately resumes downloads and schedules future discovery/review ticks without replaying missed ticks. See the [systemd user service](../deploy/openrent.service) example and [setup instructions](../README.md#service-on-linux).

## Stored data

[The schema](../src/openrent/schema.sql) has real columns, typed scalar feature rows, foreign keys, checks, and indexes. Money is stored as integer **pence**; unknown values are SQL `NULL`. A property is an OpenRent advert, keyed by its listing ID.

| Table/view | Contents |
| --- | --- |
| `properties` | OpenRent ID and canonical URL; title, full text and HTML description; property type; displayed street/address, locality, postcode, coordinates; bedrooms, bathrooms, tenant limit; monthly/weekly rent and deposit; availability and tenancy; furnishing, amenities and tenant preferences; status, first-listed time, EPC; public landlord information; first/last seen and changed timestamps |
| `property_features` | Other queryable scalar facts grouped by section: feature label/key, typed text/integer/real/boolean value, and unit; includes summary fields, landlord verification/response stats and Street View metadata |
| `property_images` | Original image URL, order, kind, caption, dimensions, download status, content hash, HTTP validators and last download error |
| `image_blobs` | **Actual image bytes as SQLite BLOBs**, MIME type, byte count and SHA-256; one copy per unique content hash |
| `nearby_places` | Nearby transport and schools, walking times and any supplied distance/coordinates |
| `media_links` | Video/tour links; video bytes are not downloaded |
| `property_summary` | Convenient view with rent in pounds and image counts |

Gallery images and the listing's static map are downloaded; site logos and UI icons are excluded. Public sources may reveal approximate coordinates and a displayed street/postcode, rather than a full door-number address. Missing facts are retained as unknown rather than inferred from marketing descriptions. Where an advert exposes changing nested source schemas, `properties.extra_metadata_json` preserves only those irregular extras; the listing itself, images, features and nearby places are normalized. A sanitized listing HTML backup preserves additional source material without login forms or request tokens.

The optional [review sidecar schema](../src/openrent/review_schema.sql) is attached under the `review` namespace. It contains `review.review_profiles` for instructions/backend/model, `review.property_reviews` for processing state and the full JSONB assessment, and `review.review_images` for the gallery used. Assessment fields are read with SQLite's `json_extract`; `json(result)` renders the JSONB BLOB as text. There is no separate findings table.

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

Run the same command again to discover new IDs and resume unfinished downloads. Completed metadata is reused; add `--refresh` to fetch updated prices, descriptions and availability. Identical imports do **not** append property, feature or image rows. First-seen timestamps stay fixed; fresh metadata updates last-seen timestamps and changed facts in place. Existing downloaded image URLs are skipped. Matching image bytes across URLs/listings share a single BLOB. Removed gallery associations disappear from the current snapshot while archived image bytes remain in the database.

The importer commits each property and image incrementally. Failed image downloads are recorded and retried on the next execution. Listings absent from a scan remain archived; only newly fetched metadata updates their availability. Existing archives migrate automatically, removing obsolete search tables while retaining listings, images and reviews. Exit code 1 indicates an error, and 130 an interruption; successful saved work is retained.

## Sources and verification

The importer uses OpenRent's public website request flow:

1. `GET /search/search_bycommutetime` resolves the location and supplies all embedded candidate IDs, coordinates, times/distances, prices and filters.
2. `GET /search/propertiesbyid?ids=...&ids=...` supplies **JSON summaries**, in the website's batches of 20. If that endpoint fails, detail pages remain the fallback.
3. `GET /{listing_id}` redirects to the canonical detail page, supplying richer metadata and the original gallery image URLs.

These are the website's internal endpoints, **not a documented supported developer API**. Search filtering is repeated locally because the initial page contains a geographic superset, and OpenRent applies many filters in JavaScript. Integer server radii are widened slightly to include boundary values; exact requested distance is then enforced using the exposed coordinates and great-circle distance. The parser rejects missing essential arrays, missing or inconsistent reported totals, mismatched lengths and unexpected units rather than treating a broken source as an empty successful search. Location and commute resolution errors are explicit. Fetching a very large area can produce many listings and a large database.

Every newly discovered matching ID in the full embedded search arrays receives a detail request; saved successful stages are reused on later runs; the first 20 visible cards and the 20-ID summary batches do not limit the scan. All discovered gallery images are downloaded unless `--skip-images` is supplied. Failed detail/image requests leave the scan incomplete and return an error. This establishes coverage of the returned search response. It does not independently establish coverage of OpenRent's entire inventory: server-side result caps, listings omitted from public search, and account-only content have not been ruled out. A server that truncates both the ID array and its reported total would not be detected by the count check. Gallery extraction uses the exposed markup; there is no independent source photo-count check, and videos are retained as links.

```sh
uv run pytest -q
uv run ruff check src tests
```

Hypothesis tests exercise our money conversion, local filters, search parser and database updates. The filter tests use a fixed generated candidate snapshot; they do not assert how OpenRent behaves across different live requests. Database tests check that repeated identical snapshots add no duplicate rows, while changed prices and descriptions update the existing listing. Focused regression tests cover sanitized real markup, async cancellation, HTTP retries, SQL constraints, cron timing and import recovery. Live checks also verified distance and London commute searches, full metadata/image downloads, and a second import adding zero properties and downloading zero images.
