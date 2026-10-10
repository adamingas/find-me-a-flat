# Find me a flat

Async Python service that discovers OpenRent flats, downloads their metadata and gallery
images into SQLite, reviews completed flats with the OpenAI Agents SDK, and optionally
emails passed or uncertain results.

## Setup

Install uv, then run `uv sync --locked --no-dev --managed-python`. The pinned Python
provides SQLite JSONB support. For the Codex backend, install the Codex CLI and log in
with `codex login --device-auth`. Alternatively, use the Responses backend with API
credentials as described in [docs/reviews.md](docs/reviews.md).

## Run the service

```sh
uv run openrent daemon \
  --cron '0 * * * *' --run-now \
  --location 'Victoria Station, London' --radius-minutes 25 \
  --rent-min 2500 --rent-max 3600 --bedrooms-min 2 --filter \
  --concurrency 1 --requests-per-minute 12 --db data/flats.sqlite \
  --criteria-file criteria.txt --review-backend codex --review-model MODEL_ID \
  --review-concurrency 3
```

There are four independent async tasks when review and notification are enabled:

- Discovery runs on `--cron`, applies your search filters, and queues unique property IDs.
- Download continuously consumes unfinished metadata and image jobs. Parsing a detail
  page adds its images to the download queue. Successful stages stay saved across restarts.
  Only completed, live flats with photos enter the durable review queue.
- Review continuously consumes that ready queue. `--review-concurrency` separately bounds
  concurrent model agents. Review has no cron schedule and makes no OpenRent requests.
- Notify runs on `--notify-cron-schedule` and emails completed, passed or uncertain reviews
  that have not been sent to each recipient. It runs independently of review and downloading.

Download and review consumers check for queued work every five seconds. Discovery always requests fresh
search HTML. All OpenRent requests in the daemon share one client, semaphore and rate
limiter, including redirects and retries. `--run-now` additionally discovers immediately;
both consumers resume saved work immediately regardless of that flag.

Omit `--criteria-file` to disable review. Rent bounds are monthly GBP. Locations are explicit
arguments: an area, address, station or postcode resolved by OpenRent. Instead of commute
minutes, use `--radius-distance 2` for a two-kilometre radius, optionally with
`--distance-unit miles`. See [the filter reference](docs/reference.md).

## Queue commands

The only commands are `daemon`, `download`, and `review`. The latter two consume one
pass of their existing queues; neither discovers properties or schedules itself.

```sh
uv run openrent download --db data/flats.sqlite --requests-per-minute 12
uv run openrent review --db data/flats.sqlite --criteria-file criteria.txt \
  --review-backend codex --review-model MODEL_ID --review-concurrency 3
```

Failed jobs remain queued. Download can be rerun without requesting completed stages.
A crash between receiving a response and committing its checkpoint can repeat that request.
Once a property is successfully reviewed, ordinary rediscovery does not review it again.
The downloader reconciles completed archives with the ready queue on startup, including
archives created before the ready queue existed.

## Email and Linux service

Add `--notify-cron-schedule '0 9 * * *' --email-to ADDRESS --env-file .env` to the daemon
for a daily email at 09:00 in `--timezone` (Europe/London by default). Repeat `--email-to`
for more recipients. Set `RESEND_TOKEN` in the file or environment. Sending uses `notifications@flats.spanashis.com`, which must be
verified in the Resend account. `--email-preview FILE` writes a digest for one recipient
without sending it. Notifications do not wait for pending or failed reviews. Delivery history
prevents duplicate notifications per property and recipient.

Edit [deploy/openrent.service](deploy/openrent.service) with your paths and model, then:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/openrent.service ~/.config/systemd/user/openrent.service
systemctl --user daemon-reload
systemctl --user enable --now openrent.service
journalctl --user -u openrent.service -f
```

Preserve the archive and its `.downloads.sqlite` and `.review.sqlite` sidecars together.
Stopping the service cancels and joins tasks before closing the HTTP client and databases.
