# Reviewing completed flats

The property workflow gathers one durable download step per image and enqueues review
after every step succeeds. Review reads the archived dossier and local gallery, checkpoints
the structured AI assessment and digest facts, then saves the review and eligible
notification item in one transaction.
Interrupting the archive write does not repeat a successfully checkpointed AI call.

`--review-concurrency` limits simultaneous agents through a separate DBOS queue. Successful
reviews are not repeated for the same property ID after criteria or model changes. There is
no application processing status or review queue table; DBOS tracks unfinished execution.

## Model configuration

The daemon accepts:

- `--criteria-file FILE`: UTF-8 instructions for the model.
- `--review-backend codex|responses`: Codex CLI login or a model API.
- `--review-model MODEL`: alternatively `OPENRENT_REVIEW_MODEL`.
- `--review-api openai|openrouter`: API for the Responses backend.
- `--review-provider PROVIDER`: optional OpenRouter provider slug; pins routing without fallback.
- `--review-timeout SECONDS`, `--review-concurrency N`.
- `--env-file FILE`: model API credentials; defaults to `.env` when present.

Codex requires the Codex CLI installed and authenticated. The Responses backend uses
`OPENAI_API_KEY` for OpenAI, or `OPENROUTER_API_KEY` (also `OPENROUTER_TOKEN`) for OpenRouter.
Environment credentials take precedence over file credentials. `OPENRENT_REVIEW_BACKEND`,
`OPENRENT_REVIEW_API` and `OPENRENT_REVIEW_PROVIDER` are also supported.

`model_config.py` defines the `ModelConfig` protocol and its OpenAI/OpenRouter implementations.
They supply asynchronous clients and Agents SDK settings. OpenRouter checks the selected
model/provider's advertised image and strict structured-output capabilities before invoking
the agent. A provider that lacks either capability fails before a review starts. API keys
are resolved inside steps and excluded from durable workflow inputs.

```sh
uv run openrent daemon --cron '0 * * * *' --run-now \
  --location 'Victoria Station, London' --radius-minutes 25 \
  --db data/flats.sqlite --criteria-file criteria.txt \
  --review-backend responses --review-api openrouter --review-model MODEL_ID \
  --review-provider PROVIDER_SLUG --env-file .env --review-concurrency 3
```

The fixed assessment fields and required conditions are defined in `review_models.py`.
The model receives the listing facts, description, features, nearby places, gallery and
criteria. SDK structured output produces the Pydantic assessment. Required criteria determine
pass/reject/uncertain locally. Unknown facts remain unknown in the result.

## Delivery

Notification is an independently scheduled DBOS workflow. Add
`--notify-cron-schedule '0 9 * * *' --email-to ADDRESS` (repeat for multiple recipients),
and set `RESEND_TOKEN` in the environment or the file supplied with `--env-file`.
The cron uses `--timezone`, defaulting to Europe/London. Notification starts at its next
scheduled tick; `--run-now` applies only to discovery.

Each tick sends prepared outbox items that have not been sent to the recipient. The payload
contains the assessment, digest facts and gallery URLs, without image bytes. Notification
does not load the property dossier, inspect download state or query DBOS system tables.
Review has no recipient configuration or sending code. Notification also works with review
disabled, sending previously queued items.
The verified sender is `notifications@flats.spanashis.com`.

`--email-preview FILE` renders the digest for one recipient without sending or recording
its delivery. `--email-timeout SECONDS` bounds provider requests. Property/recipient delivery
history prevents duplicate notifications. Failed provider submissions retain their batch
and idempotency key; ambiguous acceptance stays reserved for manual provider reconciliation.
