# OpenRent → SQLite

A `uv`-managed async Python CLI built with [Click](https://click.palletsprojects.com/en/stable/) that searches OpenRent and archives listing metadata and full-size images in a local SQLite database. No account is required for the public sources used here.

Fetching uses `asyncio` and `httpx.AsyncClient`. Listing pages and image downloads use `asyncio.gather` with a semaphore; `--concurrency` sets the maximum simultaneous HTTP requests (default **1**, range 1–32). After the search response, all summary groups and listings are scheduled with `gather`; detail fetches and image downloads share a semaphore, without sequential batch barriers. Requests start when semaphore capacity and the rate limit allow them. `--requests-per-second` (alias `--rps`) caps the shared start rate, defaults to 0.2 (one request every five seconds), and includes images, retries and redirect hops without bursts. Failed requests retry independently through one async retry decorator, with up to six attempts. `Retry-After` sets the wait when supplied; otherwise 429s wait 30, 60, 120, 240, then 300 seconds, and network/5xx errors use shorter exponential backoff. SQLite operations run on one dedicated worker thread, keeping writes serialized without blocking network requests; HTML parsing and image verification also run off the event loop. Each HTTP attempt holds the shared semaphore; backoff sleeps release its capacity. Image tasks save their bytes and return without retaining them.

`--location` is required for every search; there is no preset location. Pass an area, address, station/landmark, or postcode that OpenRent can resolve. Quote values containing spaces. For the Victoria area use `--location "Victoria, London"`; to centre specifically on the station use `--location "Victoria Station, London"`. Both have been verified against OpenRent. The resolved point is the centre of the requested radius, rather than a neighbourhood boundary. Qualify ambiguous place names with the city, or use a full postcode for a more specific centre. Raw latitude/longitude input is not supported by this CLI.

## Run on another machine

Use an updated checkout of this project and install
[uv](https://docs.astral.sh/uv/getting-started/installation/). The commands run headlessly;
the Codex desktop app and a browser on the server are not required. Use the actual GitHub
URL when cloning: the `tomoro` SSH alias configured on the development machine is local
to that machine.

```sh
git clone https://github.com/adamingas/find-me-a-flat.git
cd find-me-a-flat
uv sync --locked --no-dev --managed-python
```

`.python-version` selects Python 3.14. The Python SQLite library must support native JSONB
(SQLite 3.45 or newer); uv-managed Python avoids depending on the server's system Python.
The locked `tzdata` dependency supplies timezone data when the OS has none. Keep the database
on writable local storage; the application also creates its review sidecar, WAL files and
review lock beside it.

For review, separately [install Codex CLI](https://developers.openai.com/codex/cli) on
`PATH` and authenticate it as the same OS user that runs the review job. For a headless host,
use [device-code login](https://developers.openai.com/codex/auth#login-on-headless-devices):

```sh
codex login --device-auth
codex login status
export OPENRENT_REVIEW_MODEL="MODEL_ID"
```

Replace `MODEL_ID` with a model available to that account that supports images, structured
output and web search. Codex CLI 0.159.2 with `gpt-5.6-terra` was verified on the development
machine; model access and authentication must be configured on the new host. `uv` installs
the Agents SDK, not the Codex CLI. Edit `criteria.txt` for your instructions; the fixed
criteria and breaking/nonbreaking rules live in `src/openrent/review_models.py`.

Create a local `.env` containing `RESEND_TOKEN=your_resend_api_key`. Use a key from the Resend
account where **`flats.spanashis.com`** is verified. The sender is
**`notifications@flats.spanashis.com`**; recipients are supplied by the CLI. The `.env` file
and database files are deliberately excluded from Git.

These are the two commands to run from the project directory. The fetch example's location,
rent and bedroom bounds are editable arguments, with no Victoria preset.

**1. Fetch matching listings, metadata and every gallery image:**

```sh
uv run --locked --no-dev openrent fetch \
  --location "Victoria Station, London" \
  --radius-minutes 25 \
  --rent-min 1500 --rent-max 3500 \
  --bedrooms-min 1 --bedrooms-max 2 \
  --property-type flat --no-shared \
  --concurrency 4 --rps 1 \
  --db data/flats.sqlite
```

**2. Review new listings and email their passed or uncertain results:**

```sh
uv run --locked --no-dev openrent review \
  --db data/flats.sqlite \
  --criteria-file criteria.txt \
  --review-backend codex --review-model "$OPENRENT_REVIEW_MODEL" \
  --review-concurrency 3 --review-limit 3 \
  --env-file .env --email-to adamingas@gmail.com
```

The review example selects only **three** unprocessed listings for an initial run. Remove
`--review-limit 3` to finish the entire unprocessed queue. It waits for all selected reviews
to be saved before sending the digest, excludes rejected/already emailed properties, and
sends nothing for an empty queue or an incomplete cycle. A failed email can be retried with
the `notify` command described below, without reviewing more listings.

When moving existing data, preserve **both** `data/flats.sqlite` and
`data/flats.sqlite.review.sqlite`; the sidecar holds review and email history. Stop writers
and use SQLite backups or a checkpointed copy, so uncheckpointed WAL data is not omitted.
Starting with only the main archive loses that history and can cause repeated reviews/emails.
Do not run review jobs against separate copies of the archive if they should share that history.

This is a manual/foreground deployment. Recurring scans and reviews are supported, but no
system service, container or automatic startup is installed; see the scheduling section.
The live fetch → Codex review → Resend workflow has been verified on macOS. Another OS and
the optional Responses backend still need a live end-to-end run. For Responses, export
`OPENAI_API_KEY` in the process environment; OpenRent's `--env-file` loads email credentials
only. It does not export the model setting or OpenAI credentials.

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

[search.py](src/openrent/search.py) contains two models: `ApiFilters` for location and distance/commute radius, and `WebsiteFilters` for rent, bedrooms, features, and other locally checked choices. Every model field defaults to `None` (no constraint). The CLI supplies location, one radius, kilometres as the default unit, and live-only listings by default. Only geographic parameters go to OpenRent; website filters are checked against returned metadata before fetching details and checked again before saving. All scans feed one shared listing archive, keyed only by OpenRent ID. The destructive post-fetch rules below remain a separate stage.

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

The Python function `filter_property_ids(connection, property_ids)` in [filtering.py](src/openrent/filtering.py) reads saved metadata and returns the passing IDs. Edit that function to add hard-coded rules; its SQLite connection exposes every stored column and related table. It runs on the database worker after listings and their image downloads have been saved, before CSV export or scheduled model review. It only examines successfully imported IDs from the current scan; unrelated archived listings are left alone.

Rejected listings are physically deleted, with cascading deletion of features, nearby places, media links, image associations and reviews. Their image BLOBs are removed only when no remaining listing or review references them. Rule evaluation and deletion use one transaction, so a filter or cleanup failure rolls back the pruning. Review profiles remain. The final scan report includes the number deleted.

`--no-filter` is the default and preserves the ordinary archive behaviour. Deleted listings can be fetched again on later scans; their removed images will be downloaded again. `--filter --dry-run` is rejected because this rule requires saved detail metadata. Schedule previews with `--filter --check-schedule` remain read-only.

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

The daemon awaits scans and uses nonblocking async waits between scheduled times. Each daemon runs its own scans sequentially, while requests within each scan run concurrently. Scheduled ticks that elapse during a scan are skipped, so a slow scan never creates a queue or overlaps the next one. Scan and export errors are logged and the daemon continues on the next tick. Ctrl+C or SIGTERM cancels the current scan, joins its request tasks, closes clients and database handles, and preserves already committed properties/images. Already submitted SQLite writes and atomic CSV exports finish before shutdown. A graceful daemon shutdown returns 0; a bounded daemon run returns 1 if any attempt failed. As before, repeated scans update the same listing IDs and reuse downloaded images.

Multiple manual scans or daemons can write to the same database concurrently. SQLite serializes short write transactions; network fetching continues in parallel. Overlapping listings update the same OpenRent ID. Searches, filter settings and match histories are not stored. CSV export uses a read-only SQLite snapshot while ingestion continues.

Cron supports ordinary numeric values, comma lists, ascending ranges, positive steps, and three-letter month/day names. Sunday is 0 or 7. When both day-of-month and day-of-week are restricted, either match triggers a scan. Six/seven-field cron, macros such as `@hourly`, and extensions such as `L`, `W`, `#`, `?`, `H`, and `R` are rejected. During London's spring clock change, nonexistent local scheduled times are skipped. During the autumn change, matching times in the repeated hour can run once in each occurrence.

Keep the process and host running to maintain scanning. The command runs in the foreground and does not install an OS cron entry, launch agent, system service, or Codex automation. A terminal session or your existing process supervisor can keep it alive; after restarting, it resumes at the next future tick rather than replaying historical scans.

## Suitability review with the Agents SDK

The optional review stage uses the **OpenAI Agents Python SDK**, with two selectable async backends. Put your suitability conditions in a UTF-8 prompt file. Python turns each unprocessed listing's stored facts into a readable property dossier and supplies **every original archived gallery image**. The dossier covers price, location, rooms, availability, amenities, the description, nearby places and additional source features; database bookkeeping and HTML backups stay in the archive. Both backends can search the web to check relevant facts.

- `--review-backend codex` is the default. It uses the SDK's experimental Codex integration and the installed Codex CLI's authentication. A readable `listing.txt`, an `images.txt` manifest and all numbered images are extracted to a private temporary directory for Codex to read with its filesystem and image tools. The SDK internally runs the Codex CLI; the application has no shell wrapper or custom subprocess launcher.
- `--review-backend responses` uses an SDK `Agent` and `Runner` with the OpenAI Responses API. The readable dossier is text in the model's context, alongside each original image as a native image input with high detail. Set `OPENAI_API_KEY`; API credentials and usage are separate from Codex/ChatGPT login and subscriptions.

Supply `--review-model MODEL_ID` or set `OPENRENT_REVIEW_MODEL` for either backend. Select a model valid for that backend that supports images and structured output; the application supplies no default model. Dependencies are managed by `uv` and pinned in `uv.lock`.

```sh
uv run openrent review \
  --db data/victoria.sqlite \
  --criteria-file conditions.txt \
  --review-backend codex \
  --review-model MODEL_ID
```

The prompt file supplies additional instructions. Eight fixed criteria and their descriptions live in the Pydantic response model, whose strict schema is sent to the SDK. Each criterion reports a strict boolean outcome: `true` when its statement holds, `false` when it does not, or `null` when unknown, with supporting evidence. The three breaking conditions require no living-room carpet, at least 50 m² of internal floor area, and space for a super king bed in the primary bedroom. Python validates the assessment, then computes the decision: reject for a `false` breaking condition, uncertain for a `null` breaking condition, otherwise pass. The five nonbreaking findings do not change that decision. Every gallery image is supplied to the model. Separate area and floor results are stated numbers with evidence, estimates with certainty and evidence, or `None` when unavailable. Inspect the exact model schema with `uv run openrent review-schema`.

Review state lives in a separate SQLite sidecar: `data/victoria.sqlite` uses `data/victoria.sqlite.review.sqlite`, automatically attached as `review` by the application. The suffix is appended to the entire archive filename. Each complete assessment is stored once as native SQLite JSONB in `review.property_reviews.result`; profile and image records retain the backend, model, instructions and evidence gallery. See [query examples](docs/reviews.md#processing-records) for inspecting the results.

The same review flags work on `daemon`, which then runs **fetch → review unprocessed IDs** after
each successful full scan. By default every live, unprocessed ID is included; `--review-limit N`
bounds a cycle to N properties. The review job downloads
missing gallery images from their archived URLs before judging. It supplies every original
photograph, floorplan and map. Failed downloads or assessments leave the property unprocessed
and retryable. Each property ID is reviewed **once across all prompts**; later price,
description or picture changes update the archive but do not trigger another judgement.

See [review setup, context and scheduling](docs/reviews.md) for examples, backend details and
SQLite queries. `review --dry-run` lists all live, unprocessed IDs, including those needing image
downloads, without downloading or calling a model; no credentials or model are needed.

To email passed or uncertain flats from `notifications@flats.spanashis.com`, supply
`--email-to ADDRESS` (repeat for multiple recipients). **Resend is the default provider**.
Verify the sending domain `flats.spanashis.com` in the
[Resend dashboard](https://resend.com/docs/add-a-domain), and put the API key
in a local `.env` file:

```dotenv
RESEND_TOKEN=your_resend_api_key
```

The CLI reads `.env` if present; choose another file with `--env-file PATH`. An existing process
environment value takes precedence. `.env` and `.env.*` files are ignored by Git.
By default, the cycle reviews all live, unprocessed properties and rechecks the archive for
new arrivals. Use `--review-limit N` to restrict a cycle to at most N selected properties. It
waits for every selected review before emailing their passed or uncertain results together,
excluding rejected flats and property IDs already emailed to that recipient. Properties
outside a bounded batch remain unprocessed for later runs and do not suppress its email.
If there are no unprocessed properties, there is no email. Failed selected image downloads
or reviews remain retryable and suppress a partial digest.
`--email-preview FILE.html` writes the digest without sending or marking delivery.

```sh
uv run openrent review \
  --db data/victoria.sqlite --criteria-file conditions.txt \
  --review-backend codex --review-model gpt-5.6-terra \
  --review-limit 3 --review-concurrency 3 \
  --email-to adamingas@gmail.com --email-preview /tmp/flats-email.html
```

This example reviews only three listings using the Codex model verified on this host. Omit
`--email-preview` to send through Resend, or omit `--review-limit` to process the entire queue.
The digest includes listing links, photo
carousels, key facts and compact tables of every assessment. `--stop-after-pass` is for
review-only runs and cannot be combined with email delivery or preview. See
[email setup and delivery tracking](docs/reviews.md#review-digest-emails).

Explicitly send an existing backlog of unemailed passed or uncertain flats without running
another review, including when there are no new properties:

```sh
uv run openrent notify --db data/victoria.sqlite \
  --env-file .env --email-to adamingas@gmail.com
```

Add `--email-preview /tmp/flats-email.html` to preview instead. Cloudflare remains available with
`--email-provider cloudflare`, account ID and API token; see the optional setup in the review
documentation.

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

Export reads the shared archive. `--active-only` selects listings currently disclosed as live. Distances and commute times from a search centre are used during filtering but are not stored as listing facts; station walking times and property coordinates remain available.

For the daemon, `--export-csv FILE` refreshes all live archived listings after a successful scan. `--export-columns id,url,rent_pcm,...` selects a subset. Failed scans/exports preserve the previous CSV. Limited scans can export the pool accumulated so far; `--dry-run` cannot export. Give concurrently running daemons different output filenames when you need separate files.

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
| `property_summary` | Convenient view with rent in pounds and image counts |

Gallery images and the listing's static map are downloaded; site logos and UI icons are excluded. Public sources may reveal approximate coordinates and a displayed street/postcode, rather than a full door-number address. Missing facts are retained as unknown rather than inferred from marketing descriptions. Where an advert exposes changing nested source schemas, `properties.extra_metadata_json` preserves only those irregular extras; the listing itself, images, features and nearby places are normalized. A sanitized listing HTML backup preserves additional source material without login forms or request tokens.

The optional [review sidecar schema](src/openrent/review_schema.sql) is attached under the `review` namespace. It contains `review.review_profiles` for instructions/backend/model, `review.property_reviews` for processing state and the full JSONB assessment, and `review.review_images` for the gallery used. Assessment fields are read with SQLite's `json_extract`; `json(result)` renders the JSONB BLOB as text. There is no separate findings table.

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

Run the same command again to refresh existing properties and discover new IDs. Identical imports do **not** append property, feature or image rows. First-seen timestamps stay fixed; last-seen timestamps refresh, and changed facts update in place. Existing downloaded image URLs are skipped. Matching image bytes across URLs/listings share a single BLOB. Removed gallery associations disappear from the current snapshot while archived image bytes remain in the database.

The importer commits each property and image incrementally. Failed image downloads are recorded and retried on the next execution. Listings absent from a scan remain archived; only newly fetched metadata updates their availability. Existing archives migrate automatically, removing obsolete search tables while retaining listings, images and reviews. Exit code 1 indicates an error, and 130 an interruption; successful saved work is retained.

## Sources and verification

The importer uses OpenRent's public website request flow:

1. `GET /search/search_bycommutetime` resolves the location and supplies all embedded candidate IDs, coordinates, times/distances, prices and filters.
2. `GET /search/propertiesbyid?ids=...&ids=...` supplies **JSON summaries**, in the website's batches of 20. If that endpoint fails, detail pages remain the fallback.
3. `GET /{listing_id}` redirects to the canonical detail page, supplying richer metadata and the original gallery image URLs.

These are the website's internal endpoints, **not a documented supported developer API**. Search filtering is repeated locally because the initial page contains a geographic superset, and OpenRent applies many filters in JavaScript. Integer server radii are widened slightly to include boundary values; exact requested distance is then enforced using the exposed coordinates and great-circle distance. The parser rejects missing essential arrays, missing or inconsistent reported totals, mismatched lengths and unexpected units rather than treating a broken source as an empty successful search. Location and commute resolution errors are explicit. Fetching a very large area can produce many listings and a large database.

By default every matching ID in the full embedded search arrays receives a detail request; the first 20 visible cards and the 20-ID summary batches do not limit the scan. All discovered gallery images are downloaded unless `--skip-images` is supplied. Failed detail/image requests leave the scan incomplete and return an error. This establishes coverage of the returned search response. It does not independently establish coverage of OpenRent's entire inventory: server-side result caps, listings omitted from public search, and account-only content have not been ruled out. A server that truncates both the ID array and its reported total would not be detected by the count check. Gallery extraction uses the exposed markup; there is no independent source photo-count check, and videos are retained as links.

```sh
uv run pytest -q
uv run ruff check src tests
```

Five Hypothesis tests exercise our money conversion, local filters, search parser and database updates. The filter tests use a fixed generated candidate snapshot; they do not assert how OpenRent behaves across different live requests. Database tests check that repeated identical snapshots add no duplicate rows, while changed prices and descriptions update the existing listing. Focused regression tests cover sanitized real markup, async cancellation, HTTP retries, SQL constraints, cron timing and import recovery. Live checks also verified distance and London commute searches, full metadata/image downloads, and a second import adding zero properties and downloading zero images.
