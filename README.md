# Find me a flat

Async Python CLI that archives OpenRent listings and all gallery images in SQLite,
reviews new flats with the OpenAI Agents SDK, and emails passed or uncertain results.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Codex CLI](https://developers.openai.com/codex/cli), then:

```sh
git clone https://github.com/adamingas/find-me-a-flat.git
cd find-me-a-flat
uv sync --locked --no-dev --managed-python
codex login --device-auth
```

The project pins Python 3.14 for SQLite JSONB support (SQLite 3.45+).
Codex uses its CLI login through the Agents SDK; no OpenAI API key is needed for this backend.
It runs headlessly. `uv` installs the Python dependencies, including timezone data.

Create a local `.env`:

```dotenv
RESEND_TOKEN=your_resend_api_key
```

Use the Resend account with `flats.spanashis.com` verified; the sender is
`notifications@flats.spanashis.com`. Edit `criteria.txt` for your instructions.
The fixed criteria and breaking rules live in [review_models.py](src/openrent/review_models.py).

## Fetch

```sh
uv run --locked --no-dev openrent fetch \
  --location "Victoria Station, London" --radius-minutes 25 \
  --rent-min 1500 --rent-max 3500 \
  --bedrooms-min 1 --bedrooms-max 2 --property-type flat --no-shared \
  --concurrency 4 --rps 1 --db data/flats.sqlite
```

Locations can be an area, address, station or postcode that OpenRent resolves.
Victoria is an explicit argument. For distance instead of commute time, replace
`--radius-minutes 25` with `--radius-distance 2` (kilometres by default).
Repeated fetches reuse saved metadata and images. Add `--refresh` to request updated listing metadata.

## Review and email

```sh
uv run --locked --no-dev openrent review \
  --db data/flats.sqlite --criteria-file criteria.txt \
  --review-backend codex --review-model gpt-6-luna \
  --review-concurrency 3 --review-limit 3 \
  --env-file .env --email-to adamingas@gmail.com
```

This reviews **three** unprocessed listings. Remove `--review-limit 3` for the full queue;
change the model and recipient as needed. Each property ID is reviewed once.
The cycle waits for all selected reviews, then emails passed/uncertain flats not previously
sent to that recipient. An empty or incomplete cycle sends nothing. Both
`gpt-5.6-terra` and `gpt-6-luna` have completed live Codex reviews.

## Service on Linux

One process schedules discovery, continuously drains the SQLite download queue, and
schedules reviews independently:

```sh
uv run --locked --no-dev openrent daemon \
  --cron '0 * * * *' --review-cron '*/10 * * * *' --run-now \
  --location 'Victoria Station, London' --radius-minutes 25 \
  --rent-min 2500 --rent-max 3600 --bedrooms-min 2 --filter \
  --concurrency 1 --rps 0.2 --db data/flats.sqlite \
  --criteria-file criteria.txt --review-backend codex --review-model MODEL_ID
```

For automatic startup, edit [deploy/openrent.service](deploy/openrent.service) with your
checkout and uv paths and search settings. Add `OPENRENT_REVIEW_MODEL=MODEL_ID` to
that checkout's `.env`; include email credentials if you add `--email-to` to the unit.
Authenticate Codex as the same user who owns the service. Then install the user service:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/openrent.service ~/.config/systemd/user/openrent.service
systemctl --user daemon-reload
systemctl --user enable --now openrent.service
journalctl --user -u openrent.service -f
```

To keep the user service running after logout and start it at boot, an administrator can
run `sudo loginctl enable-linger "$USER"`. Stop it with `systemctl --user stop openrent`.
No system cron entries are needed. Download progress survives service restarts;
`--run-now` also triggers immediate discovery and review.

## Operations and reference

Keep the archive on writable local storage. When moving it, preserve **all three files**:
`data/flats.sqlite`, `data/flats.sqlite.downloads.sqlite` and `data/flats.sqlite.review.sqlite`; use SQLite backups or
stop writers and checkpoint before copying. The sidecars hold resumable download progress and review/email history.
Credentials and databases are ignored by Git.

The daemon runs in the foreground; the example systemd service must be installed explicitly.
The live workflow has been tested on macOS; Linux and the Responses backend still need
a live end-to-end check.

- [Flags, cron scheduling, storage and source coverage](docs/reference.md)
- [Review backends, output schema, email previews/retries and SQL queries](docs/reviews.md)

Use `uv run openrent COMMAND --help` for all options.
