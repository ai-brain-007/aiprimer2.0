---
name: setup
description: One-time bootstrap and health check of the AI Primer pipeline (accounts, control sheet, Drive folder tree, taxonomy, Apify formats). Use when the user says /setup, asks whether the system is ready, or after adding a new Google account.
---

# /setup — bootstrap and health check

Everything here is idempotent: running it twice changes nothing the second time.

## Steps

1. **Settings present?** Run:
   ```bash
   python -m pipeline auth check --pretty
   ```
   - If it reports `AIPRIMER_CONTROL_SHEET_ID is not set` but the summary account token is present, run
     `python -m pipeline setup create-sheet --pretty`, then tell the user to add the printed sheet id to the
     environment variables as `AIPRIMER_CONTROL_SHEET_ID` and start a new session. Stop there.
   - If a Google token or the client id/secret is missing, point the user to README.md → "Setup", step 2–3,
     and stop. Never ask the user to paste a token into the chat.
2. **Bootstrap**:
   ```bash
   python -m pipeline setup all --pretty
   ```
   This creates the control-panel tabs, the `AI Primer Raw` folder (+ `_Inbox`) in each raw account, the
   `AI Primer Summaries` folder in the summary account, imports `config/taxonomy.seed.yaml`, and prints health.
3. **Apify formats** (needs `api.apify.com` allowed and the API credential set):
   ```bash
   python -m pipeline setup fetch-apify-schemas --pretty
   ```
   If this fails with a network error, tell the user to create the "AI Primer" cloud environment as described in
   README.md → "Setup", step 5. YouTube ingestion will not work until then; file ingestion will.
4. **Report** a short health table: each account (email, free GB, status), the control sheet link, the number of
   taxonomy nodes, resources by status. Mention the two things the user can do next: `/ingest` and `/summarize`.

## Questions you may ask (AskUserQuestion)

- Only if the Accounts tab is empty and the config has no `accounts.bootstrap`: which environment variable holds
  the raw account token and which the summary token (offer the defaults `GOOGLE_REFRESH_TOKEN_RAW01` /
  `GOOGLE_REFRESH_TOKEN_SUMMARY01`).
- Whether summary Docs should be "anyone with the link can view" (default) or restricted; write the answer to
  `config/pipeline.yaml` → `docs.sharing`.

## Adding a raw account later

The user adds a new refresh-token variable (e.g. `GOOGLE_REFRESH_TOKEN_RAW02`) to the environment and a row in the
Accounts tab (`account_id=raw02, role=raw, token_env_var=GOOGLE_REFRESH_TOKEN_RAW02, priority=2`), then runs `/setup`
again: `setup all` creates the root folder for the new account and records its quota.
