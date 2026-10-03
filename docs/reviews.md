# Headless listing review

The reviewer uses the **OpenAI Agents Python SDK** through two selectable async backends. `uv` manages the `openai-agents` and `pydantic` dependencies, with resolved versions pinned in `uv.lock`.

Select the backend with `--review-backend codex|responses`; the default is `codex`. Both require a model through `--review-model MODEL_ID` or the `OPENRENT_REVIEW_MODEL` environment variable. `MODEL_ID` is a placeholder for the model you choose; select one valid for the backend that supports images and structured output. The application supplies no default model.

| Backend | Authentication and execution |
| --- | --- |
| `codex` | Installed Codex CLI with its existing authentication. Check with `codex login status` and authenticate with `codex login` when needed. Uses the Agents SDK's experimental Codex integration. |
| `responses` | `OPENAI_API_KEY` in the process environment. Uses an SDK `Agent` and `Runner` with the Responses API; API credentials and billing are separate from a Codex/ChatGPT login or subscription. |

For the Codex backend, the SDK internally starts the Codex CLI. The project uses the SDK interface without its own shell wrapper or subprocess implementation. The Responses backend calls the API directly.

## Your conditions

Create `conditions.txt` containing additional context or instructions in plain language. The fixed assessment criteria are defined in the Pydantic model; their field descriptions are sent in the schema, rather than repeated as a long rules prompt. The file supplements those criteria without changing which conditions affect the decision. Listing descriptions, pictures and web pages supply evidence rather than instructions.

Each required criterion reports `true`, `false` or `null` with supporting evidence: `true` means its statement holds, `false` means it does not, and `null` means there is insufficient evidence. Python computes the decision using only the breaking criteria: any `false` means reject; otherwise any `null` means uncertain; otherwise pass. Findings for nonbreaking criteria never change that decision. The model is instructed to inspect every supplied image.

## What the model receives

Python's dedicated SQLite worker reads a consistent listing snapshot and every current gallery image BLOB. `render_listing(snapshot, images)` turns the stored facts into a readable property dossier: identity and link, price, location, rooms, availability, tenancy, furnishing, amenities, tenant preferences, the description, nearby places, media links and relevant search facts. Additional source features are included in prose with their labels and units, so a new feature does not need a new prompt category. Raw HTML, database IDs for child rows, observation timestamps, hashes and download bookkeeping remain in the archive rather than the review prompt.

The dossier uses existing stored fields and source feature rows; it does not ask an AI to select the facts. Conflicting claims remain visible, such as a bills flag disagreeing with the description. Reported nearby walking times are labelled as source values, including zero-minute values.

An unbounded cycle includes every live, unprocessed property ID, even when its image bytes
have not yet been downloaded. Bounded cycles prepare only their selected properties. Before
judging, the review job downloads every missing gallery image from its
already archived source URL and saves the original bytes to SQLite. Existing downloaded
images are reused. Photographs, floorplans and maps are all supplied; there is no sampling.
The model runs only after the complete gallery is available, with at least one photograph.
A failed download leaves the property unprocessed and suppresses the end-of-cycle email.

For **Codex**, Python creates a private temporary directory containing readable `listing.txt`, an `images.txt` manifest mapping image numbers to filenames and all numbered original gallery image files. Codex reads the dossier and uses its filesystem and image tools to inspect the supplied image paths. Those files remain available throughout the review and are removed afterward, including on failure or cancellation.

Filesystem image inspection was verified on this host with Agents SDK 0.23.1 and `gpt-5.6-terra`: a generated PNG was supplied only as a path, and Codex correctly read its random code and three panel colours through its image viewer. The Codex backend therefore supplies paths without image attachments. This confirms that tested model/tool combination; it does not establish the same capability for every selectable model.

For **Responses**, the SDK receives the readable dossier in `input_text` and a separate native `input_image` part for each original image, with a MIME-qualified inline base64 data URL and `detail: "high"`. The pictures are image inputs in the API model's request context; their bytes are not printed as text in the dossier. That model cannot open laptop filesystem paths. No Files API upload is involved.

Both backends receive the same readable dossier and every current gallery image, including an archived static map when present. Images are numbered in order so findings can refer to them. The application does not sample, resize or silently omit images. Both can use live web search to investigate relevant facts: Codex's web search for `codex`, and the SDK's `WebSearchTool` for `responses`. Python owns database writes.

[`backends.py`](../src/openrent/backends.py) provides the shared backend interface: `BackendConfig(model=..., schema=..., instructions=..., timeout=...)`, passed to `create_backend(backend_name, config)`, where `backend_name` is `"codex"` or `"responses"`. `schema` is a Pydantic model class with an object at its root, and `instructions` is a string. Both backends expose async `.run(data, images)` and return an instance of the supplied schema. `data` accepts either readable text or a dictionary; the review job sends text, while generic dictionary callers retain their existing serialization. Listing-specific decision checks stay in the review layer. Strict validation supports schema fields such as dates and UUIDs.

Each listing gets a separate backend run. `--review-timeout` defaults to 300 seconds and `--review-concurrency` defaults to one concurrent review, with a maximum of four. If an input exceeds a model limit, or a request times out, the job fails and the listing remains unprocessed.

## Structured output

A Pydantic response type defines a strict schema for both SDK backends. Responses uses `AgentOutputSchema`, and Codex receives the corresponding schema through its SDK integration. The response contains eight fixed required criterion assessments, `area_m2`, `floor` and a summary. Each criterion supplies a strict boolean or null outcome and evidence. Each numerical field is either a stated result (`value` and `evidence`), an estimated result (also containing `certainty`), or `None` when unavailable. Estimated certainty is `high`, `medium`, `low` or `unknown`.

Stated evidence identifies the explicit number and source. Estimated evidence explains the inference or calculation, such as flooring, photographs, room dimensions or a floor plan. Area is in square metres. Floor uses ground floor `0`, first floor `1` and negative numbers for basement levels; ambiguous or split levels use `None`. The numerical types accept any finite float, including zero and negative values.

The criterion names and descriptions define the statement assessed by each boolean. Every input field is required, and unexpected fields are forbidden. Strings such as `"true"` and integer values such as `1` are rejected as outcomes. Each criterion's `breaking` flag and the final decision are computed by Python and are read-only; the model is not asked to supply them. Nonbreaking criteria and numerical findings do not independently affect the decision. Both backends use structured output rather than only asking for a particular format in the prompt.

Print the exact JSON Schema used by the SDK:

```sh
uv run openrent review-schema
```

The fixed model has this Python shape; [the source](../src/openrent/review_models.py) adds field descriptions, bounds, validators and computed read-only flags:

```python
from pydantic import BaseModel

from openrent.review_models import (
    BreakingCriterionResult,
    Certainty,
    CriterionResult,
    Evidence,
)


class StatedNumericalCriterionResults(BaseModel):
    value: float
    evidence: Evidence


class EstimatedNumericalCriterionResult(BaseModel):
    value: float
    evidence: Evidence
    certainty: Certainty


class JudgementOutput(BaseModel):
    no_living_room_carpet: BreakingCriterionResult
    bathroom_without_window: CriterionResult
    kitchen_counter_space_for_four_appliances: CriterionResult
    gas_hob_or_induction_stovetop: CriterionResult
    bedroom_carpet: CriterionResult
    area_at_least_50_m2: BreakingCriterionResult
    primary_bedroom_fits_super_king_bed: BreakingCriterionResult
    not_ground_floor: CriterionResult
    area_m2: StatedNumericalCriterionResults | EstimatedNumericalCriterionResult | None
    floor: StatedNumericalCriterionResults | EstimatedNumericalCriterionResult | None
    summary: str
```

The three breaking conditions are stated positively: no living-room carpet, at least 50 m², and a primary bedroom that fits a super king bed. `true` means the required condition holds. The five nonbreaking observations are assessed as written: for example, `bathroom_without_window` being `true` reports a windowless bathroom. Their outcomes do not decide whether the flat passes.

`CriterionResult` contains `outcome: bool | None` and `evidence`. Stated numerical results contain the number and its evidence; estimated results add certainty. Breaking flags belong to the nonnumerical criteria and are added locally to the stored result with the overall decision. They are omitted from the schema the model must fill.

Python validates the typed response, evidence text, finite numerical values and the presence of supplied photographs. It then derives the final decision. Malformed output remains retryable. All gallery images remain part of the input, with no image acknowledgement field in new outputs. Existing stored reviews retain their original JSON, including legacy string outcomes; the email renderer accepts those historical results. Completed property IDs remain processed once across prompts.

## Run modes

Review an existing archive:

```sh
uv run openrent review \
  --db data/victoria.sqlite --criteria-file conditions.txt \
  --review-backend codex \
  --review-model MODEL_ID
```

Use the Responses API instead:

```sh
uv run openrent review \
  --db data/victoria.sqlite --criteria-file conditions.txt \
  --review-backend responses --review-model MODEL_ID
```

Inspect live, unprocessed IDs, including properties needing image downloads. Add
`--review-limit N` to preview only a bounded cycle:

```sh
uv run openrent review --db data/victoria.sqlite \
  --criteria-file conditions.txt --dry-run
```

A dry run can migrate the archive; it does not register a review profile, claim or process
listings, download images or call a model, and needs no model credentials. Known
unavailable listings are excluded. By default, normal runs include all live, unprocessed IDs,
download missing galleries, then review them. Unbounded cycles check the archive again
between batches to include new arrivals. Failed jobs remain retryable.

Use `--review-limit N` to select at most N properties for a bounded cycle. That cycle finishes
when every selected property is reviewed; properties outside the selected batch remain
unprocessed and do not prevent its email. If a selected property's image download or review
fails, no partial batch email is sent. A changed criteria file, backend or model creates a new
audit profile for new/retryable listings, but **never reprocesses an already completed
property ID**.

Test just three listings, using the Codex model verified on this host:

```sh
uv run openrent review \
  --db data/victoria.sqlite --criteria-file conditions.txt \
  --review-backend codex --review-model gpt-5.6-terra \
  --review-limit 3 --review-concurrency 3 \
  --email-to adamingas@gmail.com
```

This downloads and reviews only the selected three properties, waits for all three to finish,
then sends their passed or uncertain results together. Add `--email-preview /tmp/flats-email.html`
to preview instead of sending. Omit `--review-limit` for an unbounded cycle.

For a review-only run, `--stop-after-pass` finishes as soon as one new assessment passes. It
requires `--review-concurrency 1` and cannot be combined with email delivery or preview. An
email cycle must finish its selected batch before producing the bundled digest.

## Review digest emails

Supply one or more recipients with repeated `--email-to ADDRESS` flags. A cycle downloads
missing gallery images and waits for every selected assessment to finish before sending one
concise digest per recipient. An unbounded cycle covers all live, unprocessed IDs, including
new arrivals; `--review-limit N` restricts it to at most N selected properties. Include their
**passed and uncertain** results that have not already been emailed to that recipient;
exclude rejected flats. The sender is `notifications@flats.spanashis.com`.
**Resend is the default email provider**; choose Cloudflare explicitly with
`--email-provider cloudflare`.

If there are no live, unprocessed property IDs, the automatic cycle exits without an email.
If any selected image download or assessment fails or remains incomplete, its property stays
retryable and no partial digest is sent. Unprocessed properties outside a bounded cycle's
selected batch do not suppress that batch's email.
If the complete cycle has no unemailed passed or uncertain flats, there is no email.

```sh
uv run openrent review \
  --db data/victoria.sqlite --criteria-file conditions.txt \
  --review-backend codex --review-model MODEL_ID \
  --email-to you@example.com
```

The subject is `<X> new flats found`. Each numbered listing links to OpenRent and includes a
compact table of rent, bedrooms, postcode, nearest reported Tube station and distance to
Victoria. A second table shows all eight criterion outcomes as **True**, **False** or
**Unknown**, required conditions, evidence, area and floor findings, stated or estimated
numerical provenance and certainty for estimates. The agent's summary is included. Evidence is shortened for
readability; the JSONB assessment retains the complete text. Both HTML and plain text are sent.

Each flat also includes a horizontal image carousel using its exact reviewed gallery, with
Previous/Next controls and a visible OpenRent gallery link. The first image is visible without
interaction. Controls work in the browser preview; email clients may ignore unsupported styles
or fragment navigation, so the listing link remains available. The HTML uses no JavaScript or
forms. See [Gmail's supported CSS](https://developers.google.com/workspace/gmail/design/css).

Transport distances and walking times are taken from archived listing fields. When Victoria
has no supplied distance or walking time, coordinates are used to calculate distance to
Victoria station, explicitly labelled **straight-line**. No walking distance or time is inferred.
The station coordinates are from [TfL's Victoria station record](https://api.tfl.gov.uk/StopPoint/940GZZLUVIC).
The nearest reported Tube station is selected by the shortest supplied walking time, falling
back to supplied distance when no walking times are available.

Explicitly preview an existing backlog of passed or uncertain flats without another model run:

```sh
uv run openrent notify --db data/victoria.sqlite \
  --email-to you@example.com --email-preview /tmp/flats-email.html
```

Preview mode requires exactly one recipient. It renders that recipient's unemailed passed or
uncertain flats, requires no sending credentials, and does not reserve or mark any deliveries. The
same `--email-preview` flag can be used on `review` to run the model and then write the digest.

### Resend setup

Add the exact sending domain `flats.spanashis.com` to the Resend dashboard and complete its
[domain verification](https://resend.com/docs/add-a-domain). Resend provides
the DNS records to configure. Once verified, the sender `notifications@flats.spanashis.com`
needs no separate mailbox. Create a
[Resend API key](https://resend.com/docs/dashboard/api-keys/introduction) allowed to send from
that domain.

Put the token in a local `.env` file:

```dotenv
RESEND_TOKEN=your_resend_api_key
```

The CLI reads `.env` from the working directory if present. Select another existing file with
`--env-file PATH`. A `RESEND_TOKEN` already present in the process environment takes precedence
over the file. This project uses the variable name `RESEND_TOKEN`; its value is a Resend API key.
Credential files matching `.env` or `.env.*` are ignored by Git. Credentials are read for
sending without being printed or put into the email body.

Use `notify` to send an existing backlog of unemailed passed or uncertain flats without another
model run. This is an explicit manual action and works even when there are no new properties
to review:

```sh
uv run openrent notify --db data/victoria.sqlite \
  --env-file .env --email-to adamingas@gmail.com
```

`--email-provider resend` is accepted explicitly, but is the default. Use `--email-preview FILE`
to inspect the body without sending. `--email-timeout` sets the sending timeout in seconds
(default 30).

### Optional Cloudflare provider

Choose `--email-provider cloudflare` to use Cloudflare instead. Account and token settings are
required for sending through this provider: set
`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, or pass the account ID with
`--cloudflare-account-id`. For `notifications@flats.spanashis.com` to your own inbox, **a paid
plan is not required**. Cloudflare permits direct REST sends to account-verified destination
addresses
free on any plan, including when only Email Routing is configured. Set up that free path:

1. Configure `flats.spanashis.com` as an Email Routing domain, using Cloudflare DNS. Open the
   `spanashis.com` zone's Email Routing Settings and add `flats` under Subdomains, following
   [Cloudflare's subdomain setup](https://developers.cloudflare.com/email-service/configuration/subdomains/).
   The sender must belong to that configured routing domain.
2. In the Cloudflare dashboard, go to **Compute → Email Service → Email Routing → Destination
   Addresses**. Add `adamingas@gmail.com` and open Cloudflare's verification email to activate
   the address. This is a setup requirement; the application does not verify your inbox.
3. Create an API token with **Email Sending: Edit** permission for the same Cloudflare account.
   Supply it through `CLOUDFLARE_API_TOKEN` and supply that account's ID through
   `CLOUDFLARE_ACCOUNT_ID`.

See [destination verification](https://developers.cloudflare.com/email-service/configuration/email-routing-addresses/)
and [sender-domain rules](https://developers.cloudflare.com/email-service/platform/limits/).
Sends to verified destinations do not consume monthly or daily sending quotas. For additional
CLI recipients, verify each destination first to retain the free path. Sending to arbitrary
unverified recipients requires the Workers Paid plan and an onboarded sending domain;
see [Cloudflare pricing](https://developers.cloudflare.com/email-service/platform/pricing/)
and [sending-domain setup](https://developers.cloudflare.com/email-service/get-started/send-emails/).

The application uses the [REST API](https://developers.cloudflare.com/email-service/api/send-emails/rest-api/)
directly; no Worker deployment is needed. The API token is read from the environment, never a
CLI flag. `--email-timeout` sets the sending timeout in seconds (default 30).

```sh
uv run openrent notify --db data/victoria.sqlite \
  --email-provider cloudflare --email-to adamingas@gmail.com
```

### Delivery tracking

Without `--email-to`, review commands only store assessments. Email delivery is tracked
separately for each recipient and property ID, across changes to prompts, models and listing
facts. The recipient's address is case-normalized for delivery tracking. A reserved batch keeps
its exact subject and bodies for retries. A definite API rejection leaves that batch retryable.
If a request may have reached the provider but the response is lost, delivery is marked unknown
and sending to that recipient is blocked; check the provider's logs and reconcile that batch
before retrying. This avoids automatic duplicate sends after an ambiguous failure.

## Scheduled reviews

Integrate with the scanner so reviewing follows successful scans:

```sh
uv run openrent daemon \
  --cron '*/30 * * * *' --timezone Europe/London --run-now \
  --location 'Victoria Station, London' --radius-minutes 25 \
  --rent-max 2500 --bedrooms-min 1 --no-shared \
  --db data/victoria.sqlite --criteria-file conditions.txt \
  --review-backend codex \
  --review-model MODEL_ID
```

The cron expression and search settings are examples you can replace. The daemon reviews
eligible unprocessed listings in the shared archive, including listings ingested by other
scanners. Missing gallery images are downloaded before judging; `--skip-images` is rejected
when this stage is enabled. With `--email-to`, it sends a bundle after the full review cycle
finishes; a cycle with no live, unprocessed properties exits without an email. `--review-limit`
restricts each cycle to a selected batch; an email waits for every property in that batch,
while unselected properties remain for later cycles. A failed fetch does not start a review
of its partial result, and a failed selected image download or review suppresses the partial
digest.

Alternatively, keep an existing importer separate and poll its archive:

```sh
uv run openrent review --db data/victoria.sqlite \
  --criteria-file conditions.txt --review-backend codex --review-model MODEL_ID \
  --cron '0 * * * *' --timezone Europe/London --run-now
```

Both schedules run in the foreground using the existing cron/DST rules. Keep the host/process running through your supervisor. `--check-schedule` previews five ticks without database or network changes. A review lock prevents concurrent reviewers, while an importer can update the archive concurrently. Changes to listing facts or the gallery detected before committing a judgement leave that listing unprocessed for the next attempt. The freshness fingerprint excludes observation timestamps and refreshed HTML backups, so an unchanged listing seen by another scan can still complete its review. Once a decision is committed, later changes do not trigger another review.

## Processing records

Review records are stored separately from the rental archive. For `data/victoria.sqlite`, the sidecar is `data/victoria.sqlite.review.sqlite`: `.review.sqlite` is appended to the entire archive filename. The application attaches it as `review`, giving these table names:

- `review.review_profiles`: instructions, selected backend and model.
- `review.property_reviews`: property ID, review status, fingerprint, processing timestamps and the complete assessment in one `result` column.
- `review.review_images`: references to the exact archived image gallery used.
- `review.email_batches`: recipients, rendered subject/bodies, sending status and provider response.
- `review.email_batch_items`: the property IDs reserved or sent in each batch, unique per recipient.

`result` is a native SQLite JSONB BLOB, not JSON text. It contains the complete structured output, including criterion evidence and derived decision. The `decision` and `summary` columns are virtual projections of that result, not separately stored copies. There is no separate `review_findings` table. Python validates the model output and owns the database changes.

The CLI attaches the sidecar automatically. To inspect it outside the application, use a SQLite CLI with JSONB support and open the sidecar directly:

```sh
uv run openrent stats --db data/victoria.sqlite
sqlite3 data/victoria.sqlite.review.sqlite \
  'SELECT property_id, status, processed_at, error, json(result) AS assessment FROM property_reviews;'

sqlite3 data/victoria.sqlite.review.sqlite \
  "SELECT property_id, json_extract(result, '$.summary') AS summary, json_extract(result, '$.area_m2.value') AS area_m2, json_extract(result, '$.area_m2.certainty') AS area_certainty, json_extract(result, '$.floor.value') AS floor FROM property_reviews WHERE status = 'complete' AND json_extract(result, '$.decision') IN ('pass', 'uncertain');"
```

Or attach it manually when joining results to archived listings:

```sh
sqlite3 data/victoria.sqlite <<'SQL'
ATTACH DATABASE 'data/victoria.sqlite.review.sqlite' AS review;
SELECT p.id, p.url, json_extract(r.result, '$.summary') AS summary
FROM properties AS p
JOIN review.property_reviews AS r ON r.property_id = p.id
WHERE r.status = 'complete'
  AND json_extract(r.result, '$.decision') IN ('pass', 'uncertain');
SQL
```

A completed review marks its property processed, including rejected and uncertain decisions. Failed attempts have no processing timestamp and can be retried while the property remains unprocessed. Previously processed IDs are skipped globally, even after changing conditions or refreshing listing facts.

Existing reviews migrate to the sidecar without becoming unprocessed. Older assessment payloads retain their previous shape.
