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

Start the local map services before the daemon:

```sh
docker compose up -d overpass valhalla
docker compose logs -f overpass valhalla
```

The first start imports Greater London's OpenStreetMap data into Overpass and builds
Valhalla routing tiles. Wait for `docker compose ps` to show both services as healthy.
Named volumes retain their data, and `restart: always` starts the containers again when
Docker restarts. The app resends failed public Overpass requests to
`http://127.0.0.1:8003/api/interpreter` on HTTP 429, any 5xx, timeouts or connection errors.
The Overpass image's built-in updater applies regional changes daily. To update manually:

```sh
docker compose exec --user overpass overpass /app/bin/update_overpass.sh
```

Valhalla uses `http://127.0.0.1:8002/sources_to_targets`. Both ports are exposed only on
this machine. Check Valhalla's status with
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

For a Linux system service that starts after reboot and decrypts `.env` using an Age key,
install `uv` and `sops` system-wide (in `/usr/local/bin` or `/usr/bin`) on a server with
systemd 250+ and `systemd-creds`. From wherever you cloned the repository, run:

```sh
./deploy/setup-systemd.sh
```

The installer requests sudo automatically, then prompts for the single `AGE-SECRET-KEY-1…`
line with terminal echo disabled. Paste only that line and press Enter. It passes the key
directly to `systemd-creds` through a pipe, without putting it in command arguments, exported
environment variables, or a plaintext key file. Shell tracing and core dumps are disabled.
The key is briefly held in process memory; clear your clipboard after pasting.

`.env` must already be SOPS-encrypted in dotenv
format; setup checks that it decrypts without printing its contents. Review `schedule.sh`
before installing: setup starts it immediately, including its `--run-now` work and scheduled
emails. This setup uses a native Age identity, not an SSH private key.

The installer creates a locked `openrent` account with a `nologin` shell and no extra groups.
It copies the application and encrypted `.env` into `/opt/openrent`, sets their permissions,
and encrypts the Age key into `/etc/openrent/age-key.cred`
(root-owned, mode `0600`). `uv run` manages dependencies at service startup, using the
committed lockfile, with its virtual environment, Python downloads, and cache under
`/var/lib/openrent`. Setup uses the system-installed `uv` and `sops`.
The unit uses `LoadCredentialEncrypted=age-key:...` and sets
`SOPS_AGE_KEY_FILE=%d/age-key`, pointing SOPS at the read-only runtime credential.
Setup verifies decryption through a temporary systemd service before installing the credential.
The service can write to `/var/lib/openrent` and `/opt/openrent/data`; systemd makes the rest
of its filesystem read-only. SOPS supplies the decrypted
variables to `schedule.sh` without saving a plaintext environment file. The service account
and root can still read the Age key while it is supplied to the running service.

Encryption uses `systemd-creds --with-key=auto`: TPM2 and/or the host key as available.
Without TPM protection, copying both the encrypted credential and the host key at
`/var/lib/systemd/credential.secret` allows offline decryption. Keep a secure backup of
your original Age identity for recovery on another server.

`openrent.service` starts at boot and retries after exits every 30 seconds. An explicit
`systemctl stop` keeps it stopped until it is started again or the server reboots.

```sh
sudo systemctl status openrent.service
sudo journalctl -u openrent.service -f
sudo systemctl restart openrent.service
sudo systemctl stop openrent.service
```

Rerun setup after updating the checkout, encrypted `.env`, key, or unit configuration;
uv applies lockfile changes on startup. Installed databases under `/opt/openrent/data`
are preserved. Databases in the source checkout are not automatically migrated.
Stop any previous user service or manually started daemon to avoid duplicate work and emails.
If you used an older installer that copied a plaintext key to `/etc/openrent/age-keys.txt`,
remove that unused copy after confirming the credential-based service works.
The installer does not start the local map containers described above.

Alternatively, for the original user service with a plaintext environment file,
edit [deploy/openrent.service](deploy/openrent.service) with your paths and model, then:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/openrent.service ~/.config/systemd/user/openrent.service
systemctl --user daemon-reload
systemctl --user enable --now openrent.service
journalctl --user -u openrent.service -f
```

To verify the pipeline offline, run `uv run pytest` and `uv run ruff check src tests`.
