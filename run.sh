#!/bin/sh
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

exec uv run --locked openrent daemon \
  --cron '0 */2 * * *' \
  --run-now \
  --location 'Victoria Station, London' \
  --radius-minutes 20 \
  --rent-min 2500 \
  --rent-max 3500 \
  --bedrooms-min 2 \
  --today \
  --filter \
  --concurrency 1 \
  --requests-per-minute 60 \
  --db data/flats.sqlite \
  --criteria-file criteria.txt \
  --review-backend codex \
  --review-model gpt-6-luna \
  --review-timeout 300 \
  --review-concurrency 3 \
  --timezone Europe/London \
  --notify-cron-schedule '0 9 * * *' \
  --email-to adamingas@gmail.com \
  --email-to sofia.birattari@gmail.com \
  --env-file .env
