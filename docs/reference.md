# Discovery, downloads and storage

`uv run openrent daemon --help` lists discovery filters and service options.

## Location and filters

`--location` is resolved by OpenRent: an area, address, landmark, station or postcode.
Pass Victoria explicitly as `--location 'Victoria Station, London'`. The result is a centre
and radius, rather than a neighbourhood boundary. Raw latitude/longitude input is not supported.
Choose exactly one radius mode: `--radius-minutes` for London commute time, or
`--radius-distance` for distance, in kilometres unless `--distance-unit miles` is supplied.

| Flags | Meaning |
| --- | --- |
| `--rent-min`, `--rent-max` | Inclusive monthly GBP bounds; up to two decimal places |
| `--bedrooms-min`, `--bedrooms-max` | Inclusive bounds; zero bedrooms includes studios |
| `--bathrooms-min`, `--bathrooms-max` | Inclusive bathroom bounds |
| `--property-type house/flat/room` | Repeat to include multiple types |
| `--furnishing any/furnished/unfurnished` | Required furnishing; flexible listings match either |
| `--pets`, `--students`, `--professionals`, `--families`, `--dss` | Require that tenant preference |
| `--bills-included`, `--garden`, `--parking`, `--fireplace`, `--video` | Require the feature |
| `--no-shared`, `--no-studios` | Exclude shared rooms or studio flats |
| `--move-in-before YYYY-MM-DD` | Available on or before the date |
| `--today` | First listed today, using Europe/London |
| `--max-minimum-tenancy MONTHS` | Required minimum tenancy at most this many months |
| `--include-unavailable` | Include unavailable listings returned by search |
| `--filter` | Require an Underground walk of at most 11 minutes and EPC A/B/C before image download |

`ApiFilters` contains the parameters sent to OpenRent; `WebsiteFilters` contains conditions
applied to returned metadata. Omitted website conditions are unset. Discovery applies
available facts, and downloaded detail metadata is checked against the saved conditions.
Search results missing required facts are deferred to the detail check. The optional
`--filter` rule is a pure function in `filtering.py`, evaluated before archiving the property
and downloading images. Rejected properties finish their DBOS workflow with a filtered result.

Properties that pass these checks are saved before amenity metadata is fetched. The property
workflow always calls `post_amenities_filter(property_id)` from `filtering.py` next. This
checkpointed function reads the saved JSON through SQLite and requires the shortest gym
walk to be strictly below 13 minutes and rejects a known TfL commute over 30 minutes.
Exactly 30 minutes passes; a missing or unavailable TfL estimate does not reject a property.
Edit the function to change these rules; there is no
CLI setting. A rejection stops image downloads and review; the property and
its amenity facts remain archived. Missing property coordinates or a failed/incomplete provider
response raises a retryable error. A completed lookup with no gyms, or no reachable gym,
fails the filter.

## Amenity metadata

`amenities` has exactly three columns: `property_id` (foreign key to `properties.id`),
`type` (TEXT), and `data` (SQLite JSONB stored as a BLOB). Its primary key is
`(property_id, type)`. One `gym` row contains the complete list of nearby gyms, source tags,
coordinates, pedestrian times in seconds, route distances in metres, the origin, search
radius and walking speed. Each payload is saved once and is independent of subsequent
property-detail writes. There is no automatic refresh.

`MetadataFetcher` in `amenities.py` declares `type`, `model` (a Pydantic model class),
and `async fetch(property) -> dict`. `fetch_and_store_amenities` skips already stored types,
validates new output and writes it using `jsonb(?)`. `fetch_amenities` in `ingestion.py` lists
the active fetchers inside a checkpointed DBOS step. `post_amenities_filter` in `filtering.py`
is an async function with the signature `(property_id: int) -> bool` and obtains the database
from the runtime. `await openrent.db.query(connection, sql, parameters)` runs its SQL
against the daemon's shared `aiosqlite` connection. Database access uses async functions
in `openrent.db`; a connection lock keeps complete operations from interleaving.

The gym implementation queries fitness centres, legacy `amenity=gym` and sports centres,
including multi-sport leisure centres such as Balham Leisure Centre. Yoga-only and other
single-activity studios, tennis-only facilities, and explicitly private/inaccessible facilities
are excluded. Multi-sport centres are included as gym candidates; OSM does not always
record whether they have exercise machines. Original tags and the classification reason
are retained for inspection. Pedestrian routing uses 5 km/h, a radius of at least 1.5 km
and batches of at most 25 targets. Unreachable venues are retained
with null walking times. Building-centre coordinates can differ from the actual entrance.

`GrocerMetadataFetcher` in `grocers.py` runs alongside the gym fetcher and saves a separate
`type='grocer'` payload validated by `GrocerAmenities`. Its `grocers` list contains OSM IDs,
names, coordinates, shop classifications, source tags, walking seconds and route distances.
It queries `shop=supermarket`, `convenience`, `greengrocer`, `grocery` and `food` within
1.5 km, excluding explicitly private/inaccessible shops. It uses the same 5 km/h pedestrian
matrix routing as gyms and retains unreachable shops with null times. All candidates are
stored; grocers do not add a rejection rule to `post_amenities_filter`.

Valhalla uses `https://valhalla1.openstreetmap.de/sources_to_targets` by default, with a
local fallback through `compose.yaml`. Start it with `docker compose up -d valhalla`
and wait for `docker compose ps` to report healthy before relying on the fallback. The service
downloads the Greater London extract and builds its routing graph on the first start.
The named `valhalla-data` volume retains the graph, and `restart: always` restarts the
container after exits and Docker restarts. The local matrix endpoint is
`http://127.0.0.1:8002/sources_to_targets`; the port is bound only to localhost.
The image is pinned by digest, with two CPU cores, a 4 GiB memory limit and rotated logs.
Its graph covers Greater London. To rebuild with newer map data, replace the cached PBF
and rebuild the tiles; normal app and container restarts reuse the existing graph.

Public Overpass request starts are spaced at least ten seconds apart. Public and local
Valhalla request starts are each spaced at least one second apart, with one in-flight
request per service and `Retry-After` honoured.
These budgets are shared by all property workers and separate from OpenRent's budget.

The local Overpass service in `compose.yaml` imports the Greater London extract into the
persistent `overpass-data` volume and serves the same query API on
`http://127.0.0.1:8003/api/interpreter`. It uses a pinned native ARM/x86 Docker image,
`restart: always`, four CPU cores, a 6 GiB memory limit and rotated logs. The image's
built-in updater reads the Greater London replication feed with
`OVERPASS_UPDATE_SLEEP=86400`, applying incremental changes daily. There is no custom
update scheduler. A manual update uses the existing image command:
`docker compose exec --user overpass overpass /app/bin/update_overpass.sh`.
Valhalla's routing graph retains its separate update lifecycle.

`AmenityClient` sends the same POST body to local Overpass after a public HTTP 429,
any HTTP 5xx, timeout or connection error. Further lookups use local Overpass during a
public cooldown of at least 30 seconds, extended by `Retry-After` when applicable, before
trying the public endpoint again. Local queries are serialized without the public
ten-second spacing. Successful empty results are accepted; other HTTP 4xx do not trigger
fallback. If local Overpass fails too, the error reaches the existing DBOS step retry.
Set `overpass_fallback_url=None` on `AmenityClient` to disable fallback explicitly.

Valhalla follows the same public-to-local fallback on HTTP 429, any HTTP 5xx, timeout or
connection error. Further lookups use local Valhalla during a public cooldown of at least
30 seconds, extended by `Retry-After` when applicable, before trying the public endpoint
again. Other HTTP 4xx do not trigger fallback. If local Valhalla fails too, the error reaches
the existing DBOS step retry. Set `valhalla_fallback_url=None` on `AmenityClient` to disable
Valhalla fallback explicitly.

Endpoints can be changed through its constructor. Provider references:
[Overpass Docker image](https://github.com/wiktorn/Overpass-API),
[Overpass usage policy](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html),
[Valhalla matrix API](https://valhalla.github.io/valhalla/api/matrix/), and
[OSM fitness-centre tags](https://wiki.openstreetmap.org/wiki/Tag:leisure%3Dfitness_centre).

Before the amenities filter runs, `CommuteMetadataFetcher` uses TfL's journey planner to route
from the listing coordinates to the search centre. It requests arrival by 09:00 London time
on the next weekday after the lookup date (Monday through Friday, without bank-holiday
adjustments). It saves the shortest returned public transport journey arriving by that time,
including walking and transfer time, as an `amenities` row with `type='commute'`.
The payload records the target, coordinates, planned date, duration in minutes, departure
and arrival times, and route instructions. Like other metadata, it is saved once; the email
shows its specific journey date. A failed optional lookup is reported without blocking images
or review. No public transport route produces an unavailable estimate rather than a walking
journey labelled as transit.

OpenRent's search-reported commute minutes are saved in `properties.openrent_commute_minutes`
when the property is archived, before the gym filter or optional TfL lookup. The
`search_target`, `search_target_latitude` and `search_target_longitude` columns identify
the destination. Zero-minute estimates are retained; missing estimates stay NULL.
These fetched listing facts reach the reviewer as property fields. On archive open,
existing `openrent_commute` amenity payloads are moved into these columns and removed
from `amenities`; independently fetched gym and TfL metadata stay in that table.

The shared `AmenityClient` in `amenity_client.py` spaces TfL requests at least 1.5 seconds
apart (40 per minute), below its [anonymous limit of 50 per minute](https://api-portal.tfl.gov.uk/products).
No API key is required at this rate. See the [TfL journey API](https://api.tfl.gov.uk/swagger/docs/v1).

Every saved amenity payload is loaded into review evidence and dumped as JSON into the
reviewer's context. Notifications accept an async `additional_info_generator(property_id)`
hook returning HTML, with the database bound in a function closure or callable class.
The default generator lists every gym and grocery shop less than 13 minutes away on foot
in separate tables, nearest first, with their walking times. Groceries use a closed
`details`/`summary` expander with the number of qualifying shops; clients that strip these
tags retain the visible table. Native expander support varies by email client, including
[limited support in Gmail](https://www.caniemail.com/features/html-semantics/). It runs
for previews and new email batches; retries reuse the saved batch HTML. Custom generators
are responsible for escaping dynamic values in their HTML.
The digest also reads the saved commute payload and displays its target and TfL duration
in both HTML and plain text. New batches load these facts from SQLite; retries retain their
saved email bodies.

## Requests and durable work

Discovery fetches search HTML and durably enqueues matching property workflows. Each
property workflow requests its summary and detail page, checks the saved filters, and starts
image workflows in a deterministic order. All image children must succeed before it enqueues
review. DBOS records each image download function's completion; no image response cache or
application progress flags are consulted. Image bytes are stored only in the archive.

`--requests-per-minute` / `--rpm` defaults to 12 (one request every five seconds).
`--concurrency` defaults to 1. The daemon shares these bounds across search, summary,
detail, image, redirect and retry requests. Per-request retries use six attempts, honour
`Retry-After`, and otherwise use exponential backoff. Backoff releases the HTTP semaphore.
DBOS steps additionally retry failed operations three times. Exhausted failures stay in DBOS
as errors; they do not block discovery or other properties. Use DBOS recovery controls after
repairing the cause. Interrupted workflows recover automatically on restart with the same
application version. A process lock ensures one daemon owns the shared request budget for
an archive. Image and review queues are distinct from the property queue, so a parent
waiting for its children cannot occupy the workers those children need.

The importer archives the full returned search response's matches; it cannot guarantee
OpenRent's entire inventory is present in that response. There is no listing-count limit.
Video links are retained as metadata; gallery photographs and floor plans are downloaded.

## SQLite archive

Property IDs are OpenRent IDs and primary keys. The archive stores URL, address components,
coordinates, rooms, rent and deposit, availability, tenancy, furnishing, tenant preferences,
amenities, landlord details, EPC, description and sanitized source HTML. Separate tables
store features, nearby places, amenity payloads, media links, image associations and
content-addressed image BLOBs. JSON is used for additional source metadata, amenity data
and structured model results.

Images are stored as bytes, not just URLs. Equal content shares a BLOB. Properties have
`first_seen_at`, `last_seen_at` and `updated_at` timestamps. Repeated writes update existing
rows rather than appending another property with the same ID. Successful downloaded
metadata is reused; automatic refreshing of completed jobs is intentionally absent.

The archive also stores completed assessments, image references, prepared notification
items and email delivery history. `schema.sql` defines all application tables and `db.py`
implements their storage. `<archive>.dbos.sqlite` holds DBOS workflow state and is accessed
only through the SDK. Keep the archive and DBOS system database together.
