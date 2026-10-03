# External data exports and Google Sheets

Google Sheets is a practical secondary view of this archive. SQLite should remain the source of truth for complete metadata and image bytes; a sheet can hold one row per OpenRent listing with whichever columns matter to your search. CSV export is available now. An authenticated live Sheets writer is a proposed extension, not an implemented command.

## Export choices

| Approach | Setup | Refresh behavior |
| --- | --- | --- |
| CSV → Google Sheets, Excel, or another table tool | Export locally, then import the CSV | Manual import, or let a separate integration consume the refreshed file |
| Sheets API → existing spreadsheet | Enable the Sheets API and configure credentials and spreadsheet access | The daemon can upsert selected listing columns after each successful scan |
| Another system's API | Configure that system's credentials and field mapping | Reuse the same listing-ID-based upsert design |

Export a chosen subset locally:

```sh
uv run openrent export \
  --db data/victoria.sqlite \
  --output data/victoria.csv \
  --columns id,url,title,rent_pcm,bedrooms,postcode,nearest_tube_station,nearest_tube_walk_minutes
```

`--list-columns` shows the available projection fields. `--search-id` selects one stored search and `--active-only` restricts to its current matches. The daemon's `--export-csv data/victoria.csv` and optional `--export-columns` refresh a CSV for its current search after successful scans. Import this file into Google Sheets using its File → Import menu; this is a local file export, with no automatically configured cloud upload.

Useful columns include listing ID and URL, displayed address, postcode, coordinates, monthly rent, bedrooms, bathrooms, available date, furnishing, pets, nearest Tube and rail stations and their walking minutes, main image URL, first/last seen, and current search match. Preserve unknown values as blank. Station names and walking minutes would remain OpenRent's estimates.

## Recommended unattended authentication

For a daemon, use a dedicated service account with access to an existing spreadsheet:

1. Create a Google Cloud project and enable the Google Sheets API.
2. Create a service account and obtain credentials for the machine running the daemon.
3. Create the spreadsheet in your own Google account and share it with the service account's email as an editor.
4. Configure the spreadsheet ID, tab name, and selected columns.

A service account is a separate identity and does not automatically see your spreadsheets; sharing grants access. No domain-wide delegation is needed to edit a spreadsheet directly shared with that account. A library such as `google-auth` with the official Python API client, or `gspread`, handles authentication. Keep credential files outside the repository. See [Google's server-to-server authentication guide](https://developers.google.com/identity/protocols/oauth2/service-account) and [gspread's service-account setup](https://docs.gspread.org/en/latest/oauth2.html#for-bots-using-service-account).

For a known spreadsheet ID, use the `spreadsheets` scope and do not add broad Drive scopes for file discovery. Access is constrained by what the service account can access. Google also recommends the narrower `drive.file` scope for user-authorized applications that support a per-file selection flow. Scopes apply to an entire spreadsheet, so use a dedicated spreadsheet rather than relying on a tab to isolate permissions. See [Google's Sheets scope guide](https://developers.google.com/workspace/sheets/api/scopes).

The alternative is desktop OAuth: log into your Google account once, save the refresh token locally, and refresh access tokens automatically. This requires an OAuth client and consent-screen setup. Google's example implements token caching and refresh. External OAuth applications left in Testing normally receive refresh tokens that expire after seven days, making this less convenient for an unattended daemon. See the [Python quickstart](https://developers.google.com/workspace/sheets/api/quickstart/python) and [OAuth token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

## Idempotent live synchronization design

A small adapter can use the same flat row projection as CSV export:

1. Read the sheet's header and listing-ID column at the start of each sync. Validate unique IDs and build `listing_id → current row`. Re-read this map after an ambiguous write failure; cached row numbers become wrong if someone sorts rows.
2. Batch-write existing listings to their current rows, and allocate rows for new IDs. Writing explicit new-row ranges with `spreadsheets.values.batchUpdate` is easier to retry than blindly appending again after a timeout. Grow the grid when necessary. Allow only one automated writer per tab.
3. Write only configured managed columns. Leave user columns such as Notes, Contacted, and Viewing date untouched. Unknown managed values use `""` to clear stale cells; API `null` skips a cell rather than clearing it.
4. Use `valueInputOption=RAW` so source text is never interpreted as a formula. Keep rent numeric for filtering, with conversion from SQLite's integer pence. Encode IDs consistently.
5. Commit the local scan first, then sync. A Sheets failure must not roll back the archive. Retry transient errors with backoff and retry the projection on a later scheduled run. Successful complete searches can update a `current_match` column; partial or failed searches must not mark old matches inactive.

This is an implementation proposal. Google provides [batched value writes](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate), [RAW value handling](https://developers.google.com/workspace/sheets/api/guides/values), and the documented distinction between [null and empty-string inputs](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values).

The smallest useful next implementation would add an optional Sheets dependency group, an explicit `sync-sheets` command with column selection and a preview mode, and an optional daemon post-scan sync hook. Authentication configuration and the target spreadsheet would be supplied by the user. No Google credentials or spreadsheet are needed for local scanning or CSV export.

## Images, size, quotas, and cost

Write the original image URL to the sheet; retain full image bytes in SQLite. An optional separate preview column can use a controlled formula such as `=IMAGE(V2)` referencing the image-URL cell. Google documents that `IMAGE` does not support `drive.google.com` URLs or SVGs, and source URLs may stop working when listings disappear. See the [IMAGE function documentation](https://support.google.com/docs/answer/3093333).

Export summary columns rather than full HTML or image bytes. Google currently documents a spreadsheet maximum of 20 million cells or 100 MB; its Excel-conversion guidance also removes cells over 50,000 characters. See [Google Drive file-size limits](https://support.google.com/drive/answer/37603).

Google currently documents 300 read requests and 300 write requests per minute per project, with 60 of each per minute per user per project. A service account's calls count as one user. Batched rows make a property-search refresh inexpensive in request count. Keep request payloads near or below Google's recommended 2 MB and back off on HTTP 429. Standard API use has no additional cost; Google says over-quota charging is planned later in 2026, so check the current terms before deliberately exceeding limits. See [Sheets API usage limits and pricing](https://developers.google.com/workspace/sheets/api/limits).

Sources checked on 1 October 2026. No cloud resources were created or modified for this exploration.
