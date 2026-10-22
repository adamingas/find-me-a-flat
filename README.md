# Find me a flat

Python daemon that discovers OpenRent properties, downloads their information and images,
reviews them with an AI agent, and emails passed or uncertain results on a separate schedule.
DBOS owns workflow execution, checkpoints, queues and crash recovery.

## Setup

Install uv, then run `uv sync --locked --no-dev --managed-python`. The pinned Python
provides SQLite JSONB support. For the Codex backend, install the Codex CLI and log in
with `codex login --device-auth`. Model API configuration is described in
[docs/reviews.md](docs/reviews.md).

Enable the repository's pre-commit hooks once in each checkout:

```sh
pre-commit install
```

The hooks reject staged or pushed `.env` and `.env.*` files unless their contents include
SOPS dotenv metadata and encrypted values. The check covers outgoing commit history so a
plaintext env file cannot be hidden by encrypting it in a later commit. Local Git hooks can
be bypassed, so use a server-side push rule as well if this must be enforced for every
contributor. See [.pre-commit-config.yaml](.pre-commit-config.yaml) for the hook definitions.

Start the local map services to provide fallback for public requests:

```sh
docker compose up -d overpass valhalla
docker compose logs -f overpass valhalla
```

The first start imports Greater London's OpenStreetMap data into Overpass and builds
Valhalla routing tiles. Local fallback is available once `docker compose ps` shows the
services as healthy.
Named volumes retain their data, and `restart: always` starts the containers again when
Docker restarts. The app resends failed public Overpass requests to
`http://127.0.0.1:8003/api/interpreter` on HTTP 429, any 5xx, timeouts or connection errors.
The Overpass image's built-in updater applies regional changes daily. To update manually:

```sh
docker compose exec --user overpass overpass /app/bin/update_overpass.sh
```

Valhalla uses `https://valhalla1.openstreetmap.de/sources_to_targets` first and falls back to
`http://127.0.0.1:8002/sources_to_targets` on HTTP 429, any 5xx, timeouts or connection errors.
Further requests use the local service for at least 30 seconds, extended by `Retry-After`,
before trying the public endpoint again. Both public and local Valhalla requests are spaced
at least one second apart. Both local ports are exposed only on this machine. Check
Valhalla's status with
`curl --fail http://127.0.0.1:8002/status`.

## Run

```sh
uv run openrent daemon \
  --cron '0 * * * *' --run-now \
  --location 'Victoria Station, London' --radius-minutes 25 \
  --rent-min 2500 --rent-max 3600 --bedrooms-min 2 --filter \
  --concurrency 1 --requests-per-minute 12 --db data/flats.sqlite \
  --criteria-file criteria.txt --review-backend codex --review-model MODEL_ID \
  --review-concurrency 3 \
  --notify-cron-schedule '0 9 * * *' --email-to ADDRESS --env-file .env
```

Discovery and notification have independent DBOS cron schedules in `--timezone`
(Europe/London by default). `--run-now` additionally discovers immediately.

Each discovery filters the search results and durably enqueues one property workflow per
OpenRent ID. That workflow downloads metadata, applies the remaining filters, and gathers
one download-and-store step per image in stable gallery order. After all images succeed,
the property workflow durably enqueues review. There is no application download status,
completion marker or ready queue.

The property workflow always fetches amenities after the existing filters pass, then runs
`post_amenities_filter` from `filtering.py` before images or review. Its current rule requires
a gym less than 13 minutes away on foot and rejects known TfL commutes over 30 minutes.
Gyms are looked up through OpenStreetMap/Overpass
and routed with Valhalla. `GrocerMetadataFetcher` also discovers nearby supermarkets,
convenience stores and greengrocers, with pedestrian times saved under `type='grocer'`.
Their validated JSON is saved once per property in `amenities`
and reused on recovery. Property updates leave it intact. Reviews receive every saved amenity
payload; email digests list every gym and grocery shop less than 13 minutes away on foot
in separate tables, nearest first, with their walking times.
The groceries table has a collapsed expander with the shop count. Email clients that
do not support native HTML expanders display the table expanded.

The TfL journey estimate is fetched before the amenities filter, to the search target,
arriving by 9am on the next weekday. Exactly 30 minutes passes the commute rule.
Emails show the shortest public transport journey
in minutes, including walking, with its planned arrival date. An unavailable estimate
does not stop downloads or review.
OpenRent's original search commute minutes are retained in
`properties.openrent_commute_minutes`, alongside the search target and its coordinates,
even when the TfL lookup is unavailable.

Review checkpoints the AI result before saving it with the archived evidence. A restart
recovers unfinished work using DBOS, including a property interrupted after its last image
but before review was queued. Completed properties and reviews are not repeated on later
discovery. Workflow inputs retain the original filters and criteria. Pydantic Settings loads
credentials once at startup, with process variables overriding the env file; restart to reload.

All OpenRent requests share one HTTP client, concurrency bound and rate limiter, including
redirects and retries. Review agents have their own concurrency limit. Omit
`--criteria-file` to archive without review. See [the filter reference](docs/reference.md).

DBOS exposes workflow status and recovery controls. Steps retry failures three times; exhausted failures
remain visible as DBOS errors and require repair and an explicit DBOS rewind. Interrupted
workflows recover automatically when the daemon restarts with the same DBOS application version.

## Email

Review completion saves eligible `pass` and `uncertain` results and prepared notification
items in one transaction. The notification cron reads those items and delivery history,
without loading property evidence or checking download progress. Repeat `--email-to` for
multiple recipients; set `RESEND_TOKEN` in the environment or env file. The sender is
`notifications@flats.spanashis.com` and must be verified in your Resend account.
`--email-preview FILE` writes a digest for one recipient on the notification schedule.

Delivery history retains batches and provider idempotency keys. Ambiguous email acceptance
is reserved for manual reconciliation, preventing accidental repeat delivery.

## Storage and service

`--db` contains property facts, amenity JSON, one copy of each image's bytes, completed reviews, their
image references, notification items and delivery history. Its `.dbos.sqlite` file contains DBOS workflow
state. Preserve both databases, including their WAL files when copying a running service.

The application uses one schema and storage layer for properties, reviews and notification.

### Docker Compose (Linux)

The `openrent` container runs the unchanged `schedule.sh`: discovery, reviews and
scheduled email notifications run together. Overpass and Valhalla remain separate
Compose services. Host networking preserves the application's existing localhost map URLs.

Export `OPENROUTER_API_KEY` (or `OPENROUTER_TOKEN`) and `RESEND_TOKEN` in your shell,
then build and start everything:

```sh
docker compose --env-file /dev/null up -d --build
docker compose --env-file /dev/null logs -f openrent
```

The explicit empty env file prevents Compose from loading the repository's encrypted
`.env`. Secrets come from your shell and are never copied into the image. Dependencies
are installed with uv during the build.

The `openrent-data` volume preserves the application and DBOS databases across container
replacements. Existing host databases are not automatically migrated. Containers restart
after a server reboot when Docker starts; ensure Docker is enabled at boot.
Stop any existing systemd service or manually running daemon before starting Compose
to avoid duplicate work. Startup runs the schedule's existing `--run-now` option.
Compose starts the map containers without waiting for their initial data imports;
map requests may fail until they are ready.

### Publish and pull

Push these files to GitHub, then run the **Publish container** workflow from Actions.
It builds Linux amd64 and arm64 images and pushes them to
`ghcr.io/adamingas/find-me-a-flat:latest`, plus a tag for the commit.
For a private package, authenticate the server to GHCR with an account/token that
has permission to read it.

After publishing, the server only needs `compose.yaml` and the exported environment
variables:

```sh
docker compose --env-file /dev/null pull
docker compose --env-file /dev/null up -d --no-build
```

Pulling alone downloads the image; `up` recreates the container with the new image.
Schedule and criteria changes require rebuilding the image.

To verify the pipeline offline, run `uv run pytest` and `uv run ruff check src tests`.
