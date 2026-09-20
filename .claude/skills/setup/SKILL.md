---
name: setup
description: One-time bootstrap and health check of the AI Primer pipeline (Google service account or sign-in, control sheet, Shared Drive folder tree, taxonomy, Apify formats). Use when the user says /setup, asks whether the system is ready, or after adding a new Google account or Shared Drive.
---

# /setup — bootstrap and health check

Everything here is idempotent: running it twice changes nothing the second time.

## Two credential modes

- **Service account (primary).** The key JSON is in `GOOGLE_SERVICE_ACCOUNT_JSON_RAW` / `GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY`
  (or one shared `GOOGLE_SERVICE_ACCOUNT_JSON`); `AIPRIMER_RAW_DRIVE_ID` and `AIPRIMER_SUMMARY_DRIVE_ID` name
  the place each key writes to: a Google Workspace Shared Drive id, or the id of a folder the Gmail account
  shared with the service account's email (Editor). A service account has no storage of its own, so Google is
  expected to refuse uploads into a folder of a personal Gmail Drive; `auth check` settles it with a real
  write test (`write_test.can_write`). Report Google's verdict plainly; never argue with it.
- **Refresh token (fallback for plain Gmail).** `GOOGLE_OAUTH_CLIENT_ID/SECRET` plus one
  `GOOGLE_REFRESH_TOKEN_*` per Gmail account, obtained with `pipeline auth url` / `auth exchange` or
  `scripts/auth_local.py`.

Never ask the user to paste a key or token into the chat. If they do, tell them to create a new one.

## Steps

1. **Settings present?**
   ```bash
   python -m pipeline auth check --pretty
   ```
   - `AIPRIMER_CONTROL_SHEET_ID is not set` → run `python -m pipeline setup create-sheet --pretty`.
     In service-account mode with `AIPRIMER_SUMMARY_DRIVE_ID` set it creates the sheet inside that Shared Drive;
     without a Shared Drive it prints the two options (create the drive, or create the sheet by hand in Gmail and
     share it with the service account's email). Relay the option text, ask the user to add the sheet id to the
     environment as `AIPRIMER_CONTROL_SHEET_ID`, and stop.
   - Missing key/token → point to README.md → "Setup" and stop. In refresh-token mode you may offer the in-chat
     sign-in: `python -m pipeline auth url --pretty` → the user opens the link, clicks Allow, pastes back the
     localhost address → `python -m pipeline auth exchange "<address>" --pretty` → they copy the printed
     `refresh_token` into the environment variable named in `env_var`, then start a new session.
   - Per account, read `auth_kind`, `writes_into` (kind `shared_drive` or `folder`, `reachable`), `write_test`
     and any `warning`. Unreachable → the drive/folder must be shared with the service account's email (its
     email is in the output). `write_test.can_write: false` → Google refuses uploads there; say so, quote the
     `write_test.error`, and offer the two ways out (a Workspace Shared Drive, or the Gmail sign-in below).
2. **Bootstrap**:
   ```bash
   python -m pipeline setup all --pretty
   ```
   Creates the control-panel tabs, `AI Primer Raw` (+ `_Inbox`) in the raw Shared Drive (or the raw Gmail
   Drive in refresh-token mode), `AI Primer Summaries` in the summary drive, imports `config/taxonomy.seed.yaml`,
   prints health. An account entry with `ok: false` and "Shared Drive" in the error is the quota limitation: explain
   it and stop; do not retry in a loop.
3. **Apify formats** (needs `api.apify.com` allowed and the API credential set):
   ```bash
   python -m pipeline setup fetch-apify-schemas --pretty
   ```
   On a network error, tell the user to create the "AI Primer" cloud environment as described in README.md →
   "Setup". YouTube ingestion will not work until then; file ingestion will.
4. **Report** a short health table: each account (mode, email, Shared Drive name or free GB, status), the
   control sheet link, taxonomy node count, resources by status. Mention `/ingest` and `/summarize` as next steps.

## Questions you may ask (AskUserQuestion)

- Only if the Accounts tab is empty and the environment has neither the service-account key nor refresh tokens:
  which mode the user wants (service account + Shared Drives, or Gmail sign-in).
- Whether summary Docs should be "anyone with the link can view" (default) or restricted; write the answer to
  `config/pipeline.yaml` → `docs.sharing`.

## Adding storage later

- **Service-account mode:** create another Shared Drive, add the service account as Content manager, add an
  Accounts row (`account_id=raw02, role=raw, token_env_var=GOOGLE_SERVICE_ACCOUNT_JSON, drive_id=<id>, priority=2`),
  run `/setup` again.
- **Refresh-token mode:** new Gmail, new `GOOGLE_REFRESH_TOKEN_RAW02`, Accounts row with that name, `/setup`.
