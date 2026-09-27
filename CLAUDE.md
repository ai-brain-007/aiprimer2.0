# AI Primer 2.0 — project instructions for Claude Code

This repository is the brain of a chat-driven knowledge pipeline. The user pastes resources
(YouTube links, PDFs, Word/Excel files, screenshots, video files) into a Claude Code session;
the **ingestion agent** stores them and logs them; the **summary agent** turns them into deduplicated,
fact-checked, per-author knowledge cards; a **learning layer** builds per-stage study material from the cards.

> **Read `docs/working-agreement.md` first.** It is the project's memory: who the owner is and how to talk to
> them, the security rules, the decided v2 architecture (Backblaze B2 for files, Notion for pages and logs, three
> layers), the state of the build, the decision log and the open points.
>
> **Status (2026-09-27):** the code in this repo implements v1 (Google Drive / Sheets / Docs) and was never run
> live. v2 (Backblaze + Notion) is decided and not yet built. Do not run `/setup`, `/ingest` or `/summarize`
> against live accounts until v2 lands; the sections below describe v1 and stay valid for the offline tests.

## How we work (summary; details in the working agreement)

- The owner is non-technical and steers from the chat: plain language, click-by-click steps, diagrams on request,
  honest and critical recommendations. Files dropped into the chat are the normal input.
- Secrets exist only in the cloud environment (API credentials box or variables). Never paste, echo, log, store
  or commit one; a key pasted in the chat must be rotated.
- All AI steps run on the session model; helpers are never downgraded.
- Every decision goes into the decision log with a date; update the working agreement when something changes.

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

## Where things live (v1 code; v2 targets are in the working agreement)

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

## Google credentials (v1 only; dropped in v2)

- **Refresh token (primary, for the Gmail accounts).** `GOOGLE_REFRESH_TOKEN_<ACCOUNT>` +
  `GOOGLE_OAUTH_CLIENT_ID/SECRET`; the pipeline acts as that Gmail account and uses its quota. Keys are obtained
  once with `pipeline auth url` / `auth exchange` (from the chat) or `scripts/auth_local.py`.
- **Service account (only with Google Workspace Shared Drives).** `GOOGLE_SERVICE_ACCOUNT_JSON` (or `…_RAW` /
  `…_SUMMARY`) plus `AIPRIMER_RAW_DRIVE_ID` / `AIPRIMER_SUMMARY_DRIVE_ID`. Google gives service accounts no
  storage: on a personal Gmail Drive the key can edit files shared with it but every upload or Doc creation fails
  with a quota error, which the pipeline reports as "upload refused … Shared Drive".
- `auth_kind` is detected from the value (JSON key vs token) unless set explicitly in the Accounts tab.
