# AI Primer 2.0 — project instructions for Claude Code

This repository is the brain of a chat-driven knowledge pipeline. The user pastes resources
(YouTube links, PDFs, Word/Excel files, screenshots, video files) into a Claude Code session;
the **ingestion agent** stores them in Google Drive and logs them; the **summary agent** turns them
into deduplicated, fact-checked, per-author knowledge summaries.

## The four skills (how the user talks to the system)

| Skill | What it does |
|---|---|
| `/setup` | One-time bootstrap and health check (accounts, control sheet, folder tree, Apify formats). |
| `/ingest <links, attached files, or "inbox">` | Identify, ask Domain > Primer > Stage and author, store in Drive, log. Files dropped into the chat are the normal input. |
| `/taxonomy list \| add \| rename \| sync \| move` | Manage the Domain > Primer > Stage tree and move mis-filed resources. |
| `/summarize <author>` | Build or update the author's knowledge cards and refresh the author's Google Doc. |

Read the skill file in `.claude/skills/<name>/SKILL.md` before running any of them.

## Division of labour (non-negotiable)

- **Scripts are deterministic and never call a model.** `python -m pipeline …` uploads, names, logs,
  chunks, verifies quotes, merges cards and renders. It prints ONE JSON object on stdout
  (`{"ok": true, …}` / `{"ok": false, "error": …}`), progress on stderr, exit code 0/1.
- **The agent (you) does the judgement**: metadata guesses from content, the checklist dialogue,
  transcribing screenshots/scanned pages by eye, and orchestrating helper agents for extraction,
  matching and review. Helper prompts live in `pipeline/prompts/`.
- **Helper agents get fresh context, one job each**: extraction never reviews its own output;
  the reviewer only sees `evidence.md`.
- **One model for every AI step**: helpers run on the session's model; never pass a `model` override
  to the Agent tool. The user decided this; do not downgrade helpers for cost.

## Where things live

- Control panel (registry, taxonomy, accounts, authors, summaries, jobs): the Google Sheet whose id is
  `AIPRIMER_CONTROL_SHEET_ID`, owned by the summary Gmail account. Tabs and columns: `pipeline/models.py`.
- Raw files + `.extracted.md` text versions: the raw Gmail account's Drive, `AI Primer Raw/<Domain>/<Primer>/<Stage>/`.
- Knowledge cards (source of truth for summaries): `knowledge/<author-slug>/units/U-xxxxxx.md` (see `knowledge/README.md`).
- Rendered summaries: `knowledge/<author-slug>/summary.md`, published as a Google Doc in the summary account's Drive.
- Working files: `.cache/` (downloads, Apify responses) and `.work/<author>/` (chunks, helper outputs). Both gitignored.

## Rules

1. **Never commit secrets.** Tokens live only in the cloud environment (variables / API credentials).
   Never paste, echo or log a token. Never write one into a sheet, a file or a commit.
2. **Never commit extracted text, downloads or `.work/` files.** Only `knowledge/**`, code, config and docs.
3. **IDs are permanent.** Refer to taxonomy nodes by `T-…`, authors by `A-…`, resources by their deterministic
   id (`R-YT-<video id>`, `R-F-<fingerprint>`), cards by `U-…`. Names are display only.
4. **Duplicates are skipped**, not re-ingested. `--force` only when the user explicitly asks.
5. **Cost gates.** Show the count and estimated Apify cost before ingesting a channel; above `cost_confirm_usd`
   (config) the user must confirm the amount.
6. **One author per `/summarize` session; one pipeline command at a time.**
7. **Summary Docs are AI-only.** Feedback comes through Doc comments (`pipeline doc comments`) or chat, and is
   applied to the cards, then the Doc is re-rendered.
8. **Every card needs a verbatim quote that the verifier can find in the source.** Rejected cards go to
   `knowledge/<author>/_rejected/` with a reason; never delete that folder.
9. **Commit knowledge changes** with messages like `kb(<author-slug>): v3 +12 new ~2 evolved =31 same`.
   Commit nothing else unless the user asks.

## Useful commands

```bash
python -m pipeline auth check --pretty                 # credentials + quota per account
python -m pipeline setup all --pretty                  # tabs, folders, taxonomy, health
python -m pipeline taxonomy list --pretty
python -m pipeline ingest probe <source...> --pretty   # no side effects
python -m pipeline ingest run <source> --stage "<Domain / Primer / Stage>" --author "<name or A-id>" [--title ..] [--date ..]
python -m pipeline ingest channel <url> --list --pretty
python -m pipeline resource move <R-id or title part> --stage "<path>"
python -m pipeline summarize plan --author <A-id> --pretty
python -m pytest -q                                    # all tests use in-memory fakes; no network
```

## Environment

- Python 3.11; dependencies in `requirements.txt` (installed by `scripts/setup-env.sh` in the cloud environment).
- Network: Google APIs are reachable by default; `api.apify.com` must be allowed on the environment
  (Custom network access) and the Apify token stored as an API credential for that host.
- System tools used when present: `tesseract`, `pdftoppm`, `pandoc`, `ffprobe`.

## Google credentials (two modes, chosen per account row)

- **Refresh token (primary, for the Gmail accounts).** `GOOGLE_REFRESH_TOKEN_<ACCOUNT>` +
  `GOOGLE_OAUTH_CLIENT_ID/SECRET`; the pipeline acts as that Gmail account and uses its quota. Keys are obtained
  once with `pipeline auth url` / `auth exchange` (from the chat) or `scripts/auth_local.py`. One OAuth client
  (Desktop app) serves every Gmail account. Scopes: `config/pipeline.yaml` → `google.oauth_scopes` (default
  `drive.file`: the pipeline sees only what it created; `drive` for hand-made folders and the Drive inbox).
- **Service account (only with Google Workspace Shared Drives).** `GOOGLE_SERVICE_ACCOUNT_JSON` (or `…_RAW` /
  `…_SUMMARY`) plus `AIPRIMER_RAW_DRIVE_ID` / `AIPRIMER_SUMMARY_DRIVE_ID`. Google gives service accounts no
  storage: on a personal Gmail Drive the key can edit files shared with it but every upload or Doc creation fails
  with a quota error, which the pipeline reports as "upload refused … Shared Drive".
- `auth_kind` is detected from the value (JSON key vs token) unless set explicitly in the Accounts tab.
