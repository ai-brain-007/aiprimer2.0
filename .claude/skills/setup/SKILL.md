---
name: setup
description: One-time bootstrap and health check of the AI Primer pipeline (Backblaze buckets, Notion databases under the "AI Primer" page, folder prefixes, taxonomy, Apify formats). Use when the user says /setup, asks whether the system is ready, or after adding a Backblaze account or changing the Notion page.
---

# /setup — bootstrap and health check

Everything here is idempotent: running it twice changes nothing the second time. Every command prints JSON.

## Modes

- **v2 (current): Backblaze + Notion.** On when `AIPRIMER_NOTION_PAGE_ID` (the "AI Primer" page) and at least one
  `B2_KEY_ID_000N` / `B2_APPLICATION_KEY_000N` pair are set. The Notion secret and the Apify token live in the
  environment's API credentials box (hosts `api.notion.com`, `api.apify.com`); the pipeline never sees them.
  Each Backblaze key pair becomes a storage account `b2-000N` with the private bucket `ai-primer-raw-000N`
  (override with `B2_BUCKET_000N`). A public media bucket is optional (`B2_MEDIA_BUCKET_000N`); without it,
  pictures are uploaded into Notion and videos are embedded from YouTube.
- **v1 (legacy): Google Drive / Sheets / Docs.** Kept for the offline tests; only used when no v2 variable is set.

Never ask the user to paste a key or token into the chat. If they do, tell them to create a new one.

## Steps

1. **Settings and access**
   ```bash
   python -m pipeline auth check --pretty
   ```
   - `control_panel.ok: false` → the Notion page id is wrong, the integration is not connected to the page
     (page menu → Connections → "AI Primer pipeline"), `api.notion.com` is not allowed, or the secret is not in the
     API credentials box. Say which, using the `hint`, and stop.
   - Per Backblaze account read `writes_into.reachable`, `write_test.can_write`, `used_gb`, `cap_gb`, `status`,
     and `media.public`. Unreachable bucket → wrong bucket name or a key not allowed on it. `can_write: false`
     with a cap message → the free 10 GB are used: the account is marked full; the next account is needed.
     `media.configured: false` is normal (no public bucket). `media.public: false` → a configured media bucket
     is not Public, so pictures served from it would not display.
   - Missing variables → point to README.md → "Setup" and stop.
2. **Bootstrap**
   ```bash
   python -m pipeline setup all --pretty
   ```
   Creates the databases under the "AI Primer" page (Accounts, Taxonomy, Folders, Resources, Authors, Summaries,
   Jobs), the `raw/` and `raw/_Inbox/` prefixes in each raw bucket, imports `config/taxonomy.seed.yaml`, prints
   health. Folders inside the bucket are named by node id (`raw/<domain id>/<primer id>/<stage id>/`), so renaming a stage never
   touches storage; the readable path is in the Resources database.
3. **Apify formats** (needs `api.apify.com` allowed and the credential set):
   ```bash
   python -m pipeline setup fetch-apify-schemas --pretty
   ```
   On a network error, tell the user to check the environment's allowed domains and API credentials. YouTube
   ingestion will not work until then; file ingestion will.
4. **Report** a short health table: mode, each storage account (bucket, used GB of cap, status, media public?),
   the Notion page link, taxonomy node count, resources by status. Mention `/ingest` and `/summarize` as next steps.

## Questions you may ask (AskUserQuestion)

- Whether summary pages may be shared publicly from Notion (default: no, the owner shares by hand); write the
  answer to `config/pipeline.yaml` → `docs.sharing`.

## Adding storage later (free-tier rollover)

Create Backblaze account 000N with its private bucket and a key allowed on it; add `B2_KEY_ID_000N` and
`B2_APPLICATION_KEY_000N` (and `B2_BUCKET_000N` if the default name was taken) to the environment; start a new
session; run `/setup`. The row `b2-000N` is added with the next priority and receives uploads once the earlier
accounts are full.
