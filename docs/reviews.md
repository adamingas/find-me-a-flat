# Reviewing completed flats

Review consumes the explicit ready queue populated by the downloader. It does not discover
flats, check download progress, fetch photographs, or schedule itself. The model adapter
reads the queued flat's archived dossier and local gallery evidence. The model's structured
assessment is stored with its audit trail; completed IDs are removed from the queue.

The daemon runs review continuously alongside discovery and downloading. The `review`
command consumes one existing queue pass. `--review-concurrency` sets the independent
limit on simultaneous agents; `--review-limit` bounds the number selected per pass.
Failures and interruptions remain retryable. Successful reviews are not repeated for the
same property ID after criteria or model changes.

## Model configuration

Both daemon and review accept:

- `--criteria-file FILE`: UTF-8 instructions for the model.
- `--review-backend codex|responses`: Codex CLI login or a model API.
- `--review-model MODEL`: alternatively `OPENRENT_REVIEW_MODEL`.
- `--review-api openai|openrouter`: API for the Responses backend.
- `--review-provider PROVIDER`: optional OpenRouter provider slug; pins routing without fallback.
- `--review-timeout SECONDS`, `--review-concurrency N`, `--review-limit N`.
- `--env-file FILE`: model API credentials; defaults to `.env` when present.

Codex requires the Codex CLI installed and authenticated. The Responses backend uses
`OPENAI_API_KEY` for OpenAI, or `OPENROUTER_API_KEY` (also `OPENROUTER_TOKEN`) for OpenRouter.
Environment credentials take precedence over file credentials. `OPENRENT_REVIEW_BACKEND`,
`OPENRENT_REVIEW_API` and `OPENRENT_REVIEW_PROVIDER` are also supported.

`model_config.py` defines the `ModelConfig` protocol and its OpenAI/OpenRouter implementations.
They supply asynchronous clients and Agents SDK settings. OpenRouter checks the selected
model/provider's advertised image and strict structured-output capabilities before claiming
queued flats. A provider that lacks either capability fails before a review starts. API keys
are excluded from configuration representations and audit identity.

```sh
uv run openrent review --db data/flats.sqlite --criteria-file criteria.txt \
  --review-backend responses --review-api openrouter --review-model MODEL_ID \
  --review-provider PROVIDER_SLUG --env-file .env --review-concurrency 3
```

The fixed assessment fields and required conditions are defined in `review_models.py`.
The model receives the listing facts, description, features, nearby places, gallery and
criteria. SDK structured output produces the Pydantic assessment. Required criteria determine
pass/reject/uncertain locally. Unknown facts remain unknown in the result.

## Delivery

Notification is an independent async task in the daemon. Add
`--notify-cron-schedule '0 9 * * *' --email-to ADDRESS` (repeat for multiple recipients),
and set `RESEND_TOKEN` in the environment or the file supplied with `--env-file`.
The cron uses `--timezone`, defaulting to Europe/London. Notification starts at its next
scheduled tick; `--run-now` applies only to discovery.

Each tick sends completed passed or uncertain reviews that have not been sent to the
recipient, using the saved assessment and gallery links. Pending or failed reviews do
not block delivery. Review has no notification configuration, sending code or email flags.
Notification also works with review disabled, sending previously stored results.
The verified sender is `notifications@flats.spanashis.com`.

`--email-preview FILE` renders the digest for one recipient without sending or recording
its delivery. `--email-timeout SECONDS` bounds provider requests. Property/recipient delivery
history prevents duplicate notifications. Failed provider submissions retain their batch
and idempotency key; ambiguous acceptance stays reserved for manual provider reconciliation.
