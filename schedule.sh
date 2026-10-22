#!/usr/bin/env bash
set -euo pipefail

exec uv run openrent daemon \
  --cron '0 */2 * * *' \
  --notify-cron-schedule '0 10 * * *' \
  --timezone Europe/London \
  --run-now \
  --location 'Victoria Station, London' \
  --radius-minutes 20 \
  --rent-min 2500 \
  --rent-max 3500 \
  --bedrooms-min 2 \
  --filter \
  --today \
  --requests-per-minute 60 \
  --db data/flats.sqlite \
  --criteria-file criteria.txt \
  --review-model openai/gpt-6-luna \
  --review-api openrouter \
  --review-backend responses \
  --email-to adamingas@gmail.com \
  --email-to sofia.birattari@gmail.com \
  "$@"
