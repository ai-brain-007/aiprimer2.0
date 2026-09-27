# AI Primer 2.0 — project instructions for Claude Code

This repository is the brain of a chat-driven knowledge pipeline. The user pastes resources
(YouTube links, PDFs, Word/Excel files, screenshots, video files) into a Claude Code session;
the **ingestion agent** stores them and logs them; the **summary agent** turns them into deduplicated,
fact-checked, per-author knowledge cards; a **learning layer** builds per-stage study material from the cards.

> **Read `docs/working-agreement.md` first.** It is the project's memory: who the owner is and how to talk to
> them, the security rules, the architecture (Backblaze B2 for files, Notion for pages and logs, three layers),
> the writing style, the state of the build, the decision log and the open points.

## How we work (summary; details in the working agreement)

- The owner is non-technical and steers from the chat: plain language, click-by-click steps, diagrams on request,
  honest and critical recommendations. Files dropped into the chat are the normal input.
- Secrets exist only in the cloud environment (API credentials box or variables). Never paste, echo, log, store
  or commit one; a key pasted in the chat must be rotated.
- All AI steps run on the session model; helpers are never downgraded.
- Everything a reader sees follows `pipeline/prompts/style.md` (sample: `docs/style-sample-jab.md`).
- Every decision goes into the decision log with a date; update the working agreement when something changes.

## The four skills (how the user talks to the system)

| Skill | What it does |
|---|---|
| `/setup` | One-time bootstrap and health check (Backblaze buckets, Notion databases, folder prefixes, taxonomy, Apify formats). |
| `/ingest <links, attached files, or "inbox">` | Identify, ask Domain > Primer > Stage and author, store in Backblaze, log in Notion. Files dropped into the chat are the normal input. |
| `/taxonomy list \| add \| rename \| sync \| move` | Manage the Domain > Primer > Stage tree and move mis-filed resources. |
| `/summarize <author>` | Build or update the author's knowledge cards and rewrite the author's Notion page. |

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

## Where things live (v2)

- Control panel (accounts, taxonomy, folders, resources, authors, summaries, jobs): Notion databases under the
  page whose id is `AIPRIMER_NOTION_PAGE_ID`, each inside its layer page (`pipeline/layout.py`: LAYER 1 - RAW
  MATERIAL, LAYER 2 - SUMMARY BY AUTHORS, LAYER 3 - DOMAINS > PRIMERS > STAGES with one page per taxonomy node,
  LAYER 0 - CONFIG). Columns: `pipeline/models.py`; store: `pipeline/notion.py`. `setup layout` is idempotent.
- Drawings go into pages, never on a page of their own: a ```mermaid code block in markdown renders as a diagram in
  Notion (domain and primer pages carry their subtree, redrawn by `sync_tree_pages`); a bare excalidraw.com link on
  its own line becomes an embedded board. The publisher refreshes a page in place above its child pages.
- Raw files + `.extracted.md` text versions: Backblaze bucket `ai-primer-raw-000N` (private), keys
  `raw/<domain id>/<primer id>/<stage id>/<readable filename>`; `raw/_Inbox/` for hand-uploaded files. Client: `pipeline/storage_b2.py`.
  Pictures go into Notion (5 MB each) and videos are YouTube embeds; a public media bucket is optional.
- Knowledge cards (source of truth for summaries): `knowledge/<author-slug>/units/U-xxxxxx.md` (see `knowledge/README.md`).
- Rendered summaries: `knowledge/<author-slug>/summary.md`, published into the author's row page of the Notion
  Authors database (same link every version).
- Working files: `.cache/` (downloads, Apify responses) and `.work/<author>/` (chunks, helper outputs). Both gitignored.
- Reference pages for the owner ("Command guide", "How the pipeline works"): written in `docs/notion/*.md`, published
  into the "LAYER 0 - CONFIG" page under "AI Primer" by `python -m pipeline doc guide`. Rule 11: update them with
  every change the owner would notice; the gate `scripts/hooks/guide_gate.py` checks it before each push.
- Legacy v1 (Google Drive / Sheets / Docs) code remains in `pipeline/drive.py`, `sheets.py`, `docs.py`,
  `google_auth.py`; it is used only when no v2 variable is set and is covered by the offline tests.

## Rules

1. **Never commit secrets.** Tokens live only in the cloud environment (variables / API credentials).
   Never paste, echo or log a token. Never write one into a page, a database, a file or a commit.
2. **Never commit extracted text, downloads or `.work/` files.** Only `knowledge/**`, code, config and docs.
3. **IDs are permanent.** Refer to taxonomy nodes by `T-…`, authors by `A-…`, resources by their deterministic
   id (`R-YT-<video id>`, `R-F-<fingerprint>`), cards by `U-…`, storage accounts by `b2-000N`. Names are display only.
4. **Duplicates are skipped**, not re-ingested. `--force` only when the user explicitly asks.
5. **Cost gates.** Show the count and estimated Apify cost before ingesting a channel; above `cost_confirm_usd`
   (config) the user must confirm the amount.
6. **One author per `/summarize` session; one pipeline command at a time.**
7. **Author pages are AI-only.** Feedback comes through Notion comments (`pipeline doc comments`) or chat, and is
   applied to the cards, then the page is republished.
8. **Every card needs a verbatim quote that the verifier can find in the source.** Rejected cards go to
   `knowledge/<author>/_rejected/` with a reason; never delete that folder.
9. **Commit knowledge changes** with messages like `kb(<author-slug>): v3 +12 new ~2 evolved =31 same`.
   Commit nothing else unless the user asks.
10. **Storage stays free by rollover.** A free Backblaze account refuses uploads past 10 GB; the pipeline marks
    it full and uses the next `b2-000N` row. Never delete files to make room.
11. **The owner's reference pages stay current.** "Command guide" and "How the pipeline works" (`docs/notion/*.md`,
    published by `python -m pipeline doc guide`) must describe the pipeline as it is. Whenever you change a command,
    a skill, the process or a rule the owner sees, update the markdown in the same commit and republish before you
    push. Before every `git push`, run the gate yourself: `python3 scripts/hooks/guide_gate.py --check`. It fails
    when the push changes `pipeline/`, `.claude/skills/` or `config/` without touching `docs/notion/` (unless a
    commit message in the push says `Guide: unchanged`, written only after you checked the pages), and it
    republishes the pages when `docs/notion/` changed. When the owner has registered it as a PreToolUse hook in
    `.claude/settings.json`, it runs on its own before each push.

## Useful commands

```bash
python -m pipeline auth check --pretty                 # access, write test, used space per account; Notion reachability
python -m pipeline setup all --pretty                  # databases, bucket prefixes, taxonomy, layer layout, health
python -m pipeline setup layout --pretty               # layer pages, tables in their layer, Domain > Primer > Stage pages
python -m pipeline taxonomy list --pretty
python -m pipeline ingest probe <source...> --pretty   # no side effects
python -m pipeline ingest run <source> --stage "<Domain / Primer / Stage>" --author "<name or A-id>" [--title ..] [--date ..]
python -m pipeline ingest channel <url> --list --pretty
python -m pipeline resource move <R-id or title part> --stage "<path>"
python -m pipeline resource refresh-links [--all] --pretty    # renew the 7-day links of the Resources rows (link, text_link)
python -m pipeline summarize plan --author <A-id> --pretty
python -m pipeline doc comments --author <A-id> --pretty
python -m pipeline doc guide --pretty                  # republish docs/notion/*.md as reference pages under "AI Primer"
python3 scripts/hooks/guide_gate.py --check            # before git push: guides updated? republished? (rule 11)
python -m pytest -q                                    # all tests use in-memory fakes; no network
```

## Environment

- Python 3.11; dependencies in `requirements.txt` (installed by `scripts/setup-env.sh` in the cloud environment).
- Network (Custom): `api.apify.com`, `*.backblazeb2.com`, `*.backblaze.com`, `api.notion.com`, plus the default
  package managers. The Apify token and the Notion secret are API credentials of the environment (attached by the
  proxy for their hosts); the Backblaze key pair is in the variables `B2_KEY_ID_000N` / `B2_APPLICATION_KEY_000N`.
- System tools used when present: `tesseract`, `pdftoppm`, `pandoc`, `ffprobe`.
- Notion API version is pinned to 2022-06-28 (`config/pipeline.yaml`); the client throttles to about 3 calls/s.
