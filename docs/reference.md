# Discovery, downloads and storage

`uv run openrent daemon --help` lists discovery filters and service options.
`download` only consumes previously queued work; it accepts database and HTTP settings.

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
| `--filter` | Delete flats unless nearest Tube walking time is at most 11 minutes and EPC is A/B/C |

`ApiFilters` contains the parameters sent to OpenRent; `WebsiteFilters` contains conditions
applied to returned metadata. Omitted website conditions are unset. Discovery applies
available facts, and downloaded detail metadata is checked against the saved conditions.
The destructive `--filter` rule is saved with each newly queued property and runs before
its images are downloaded. Deleted IDs remain completed in the download queue.

## Requests and durable work

Discovery fetches search HTML and queues every matching ID in its embedded arrays.
Downloading requests summaries in groups of up to 20 IDs, then individual detail pages.
Parsing a detail page durably adds all its gallery image jobs. Pending images resume from
their saved URLs without requesting the detail page or successful summaries again.
Completed stages are reused, and repeated discovery does not duplicate queue rows.

`--requests-per-minute` / `--rpm` defaults to 12 (one request every five seconds).
`--concurrency` defaults to 1. The daemon shares these bounds across search, summary,
detail, image, redirect and retry requests. Per-request retries use six attempts, honour
`Retry-After`, and otherwise use exponential backoff. Backoff releases the HTTP semaphore.
`download` uses its own client when run separately; a process lock prevents it overlapping
the daemon for the same archive.

Only the downloader decides whether an archive is ready. It publishes a property ID into
`review_queue` after its complete gallery is stored. Review consumes IDs from that table;
failed/interrupted reviews remain queued, and successful reviews are removed.

The importer archives the full returned search response's matches; it cannot guarantee
OpenRent's entire inventory is present in that response. There is no listing-count limit.
Video links are retained as metadata; gallery photographs and floor plans are downloaded.

## SQLite archive

Property IDs are OpenRent IDs and primary keys. The archive stores URL, address components,
coordinates, rooms, rent and deposit, availability, tenancy, furnishing, tenant preferences,
amenities, landlord details, EPC, description and sanitized source HTML. Separate tables
store features, nearby places, media links, image associations and content-addressed image
BLOBs. JSON is reserved for additional changing source metadata and structured model results.

Images are stored as bytes, not just URLs. Equal content shares a BLOB. Properties have
`first_seen_at`, `last_seen_at` and `updated_at` timestamps. Repeated writes update existing
rows rather than appending another property with the same ID. Successful downloaded
metadata is reused; automatic refreshing of completed jobs is intentionally absent.

The `.downloads.sqlite` sidecar holds durable inputs and successful stage checkpoints.
The `.review.sqlite` sidecar holds decisions, their audit evidence and notification history.
The archive's `review_queue` cascades when its property is deleted. Existing databases
upgrade automatically; preserve all three files together.
