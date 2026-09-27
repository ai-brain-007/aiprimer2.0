# How we work on AI Primer 2.0

This is the project's memory. Every Claude Code session reads `CLAUDE.md`, which points here. Update this file
whenever a decision is taken or changed, with the date. Never write a secret in it.

Last updated: 2026-09-27.

## 1. Who and how

- The owner is Jules, a non-technical stakeholder who directs the project from the chat. Explain things in plain
  language, the way you would to a stakeholder, with click-by-click steps when a setting has to be changed.
  Read the screenshots Jules pastes and answer from what they show.
- Be honest and critical. Give a recommendation with its reasons, not a survey. Say plainly when something the
  owner wants will not work, then build what was asked if the owner confirms.
- Draw when asked: the owner likes diagrams of who does what (owner, agent, helpers, scripts, storage).
- Resources arrive by being dropped into the chat (files) or pasted (links). That is the normal input path.
- All AI work runs on the session's model. Helpers are never downgraded to a cheaper model.
- Decisions are logged in section 7 with a date. Do not reopen a logged decision unless the owner does.

## 2. Security rules (absolute)

- Secrets live only in the Claude Code cloud environment: host-scoped tokens in the **API credentials** box
  (the proxy attaches them; the agent never sees them), everything else in the environment variables.
- Never paste, echo, log or commit a token or key. Never write one into a file, a database, a Notion page or a
  commit. Never ask the owner to paste a key into the chat; if one is pasted, it must be rotated.
- Screenshots that show a private key line are a leak; warn the owner.
- The owner rotated the Apify token that was pasted in the first session. Apify token is stored as an API
  credential for host `api.apify.com` (name "Apify", header Authorization, prefix Bearer).

## 3. Architecture (v2, decided 2026-09-27)

Three layers. Layer 3 never reads raw data; it reads only layer 2 cards.

| Layer | What | Where |
|---|---|---|
| 1. Raw | Original files, extracted text, audio + key frames for videos, per-file metadata | Backblaze B2 buckets; index in the Notion Resources database |
| 2. Per author | Verified knowledge cards (one idea each) and the author page rendered from them | Cards in the repo `knowledge/<author>/units/`; pages in Notion |
| 3. Per Domain → Primer → Stage | Stage brief, SOPs, practice, lists, scripts, glossary crosswalk, self-test, built from all authors' cards | Notion Stages database; built in dialogue with the owner |

**Accounts (identifiers, not secrets).**

| Purpose | Account | Notes |
|---|---|---|
| Backblaze owner | `ai.primer.rawfile.0001@gmail.com` | Backblaze account `b2-0001`; buckets `aiprimer-rawfile-0001` (Private, encryption on; `B2_BUCKET_0001`) and `aiprimer-media-0001` (Public, for excerpts shown in pages; `B2_MEDIA_BUCKET_0001`). The owner paid Backblaze's one-time $1 fee (credited) to unlock public buckets. Originals never go into the public bucket |
| Notion owner | the owner's personal Google account (not written here) | Separate single-member workspace "AI Primer", top page "AI Primer", integration "AI Primer pipeline" |
| Retired | `ai.primer.summary.0001@gmail.com`, `ai.primer.brain.0001@gmail.com` | The first was for Google Sheets/Docs (v1); the second was suspended by Google right after creation (new accounts created in quick succession get suspended). Do not create more throwaway Gmail accounts; for a future Backblaze account use a plus-address of the personal Gmail (`…+b2-0002@gmail.com`) |

**Storage policy.** The owner wants storage to stay free: Backblaze gives 10 GB per account, so the pipeline
keeps a Storage-accounts table (`b2-0001`, `b2-0002`, …) and rolls over to the next account when one refuses an
upload. To make 10 GB last, videos are stored as audio track plus a few key frames unless the owner marks a video
"keep full". The agent's recommendation to pay about $7 per TB per month instead of chaining accounts is on record;
the owner declined. Backblaze's terms on multiple free accounts have not been verified from the sandbox.

**Layer 1 metadata** is written three times by the same step: the Notion Resources row, a `R-xxx.meta.json`
sidecar next to the file in the bucket (plus file-info stamps on the object), and a JSON export in the repo.

**Layer 2 cards** have these types: principle, concept, technique/procedure, drill/exercise, list, script,
example/case, claim, mistake/anti-pattern, glossary term. Each carries canonical name + aliases, the author's
conditions (for whom, when), stage tags, dates, and at least one verbatim quote with page or timestamp. Evolution
over time is tracked per card (dated versions, statuses current/superseded/retracted/contradicted); it requires
source dates, so ingest asks for a date when none is found.

**Notion layout (decided 2026-09-27, `pipeline/layout.py`).** Top page "AI Primer" → one page per layer, in this
order: `LAYER 1 - RAW MATERIAL` (Resources),
`LAYER 2 - SUMMARY BY AUTHORS` (Authors with the rendered author page inside each row, Summaries as version history),
`LAYER 3 - DOMAINS > PRIMERS > STAGES` (one page per taxonomy node, nested domain > primer > stage, generated from
the Taxonomy table; the layer 3 artefacts will be published into the stage pages, the way author pages are
published into Authors rows), `LAYER 0 - CONFIG` last (Accounts, Taxonomy, Folders, Jobs, and the two reference
pages for the owner, "Command guide" and "How the pipeline works", generated from `docs/notion/*.md` by
`python -m pipeline doc guide` and refreshed in place). The Notion API cannot move a database, so `setup layout` relocates a table by creating it in its layer page, copying the rows, verifying
the count and archiving the old one; the registry finds its tables inside the layer pages. A Cards database (one
library for all authors) is still planned. The reference pages are the owner-facing documentation: update the
markdown and republish whenever a command or the process changes. Pictures under 5 MB are
uploaded into Notion; videos and large media are shown from the public bucket or as YouTube embeds. Pages are
generated: humans give feedback through Notion comments or the chat, the pipeline applies it to the cards and
republishes only the changed cards.

**Layer 3 selection policy.** Agreement across independent authors weighs most, then specificity, then recency,
then depth of the source; the owner can pin an author as primary for a primer. Every sentence cites a card. A gap
triggers a focused layer 2 re-extraction, never a read of raw data. Stage artefacts record the card versions they
used and are marked stale when those cards change.

**Drawing.** Notion has no native infinite canvas; embed Excalidraw/tldraw for sketching, or Miro if the board must
be readable by the agent through an API.

## 3b. Writing style for everything the owner reads (decided 2026-09-27)

- Conversational, like a good chat answer: talk to the reader as "you", short sentences, no jargon without a
  one-line explanation.
- Written for a newcomer first: what it is, why it matters, how to do it, what goes wrong, how to practise, then depth.
- Straight to the point: lead with the answer, one idea per paragraph, no filler, no hype, no repetition.
- Concrete: numbers, cues, examples, and in pipeline output a verified quote with page or timestamp behind every claim.
- Layered: one line, one paragraph, full details, so the reader can stop at any depth.
- Approved sample: `docs/style-sample-jab.md`. The operative guide for helpers and renderers is
  `pipeline/prompts/style.md`; the extraction, consolidation and review prompts, the summarize skill and the
  summary template follow it. Layer 3 builders must follow it too when they are written.

## 4. Engineering rules

- Scripts (`python -m pipeline …`) are deterministic and never call a model; they print one JSON object.
  The agent does the judgement; helper agents get fresh context and one job each.
- Tests run offline against fakes (`python -m pytest -q`). Add a fake for every external service.
- Branch: `claude/jolly-davinci-wdvr3o` (earlier sessions: `claude/determined-ritchie-771mo0`). Commit and push after each coherent change. Knowledge commits use
  `kb(<author-slug>): vN +new ~evolved =same`.
- Cloud environment "AI Primer 2.0": Custom network access, allowed domains `api.apify.com`,
  `*.backblazeb2.com`, `*.backblaze.com`, `api.notion.com`, plus the default package managers. The setup box
  does not start in the repository folder, must finish in about five minutes and must exit 0; `scripts/setup-env.sh`
  is written for that. The Google variables can be removed once v2 lands.
- IDs are permanent: `T-…` taxonomy nodes, `A-…` authors, `R-YT-<video id>` / `R-F-<fingerprint>` resources,
  `U-…` cards, `b2-000N` storage accounts.
- The owner's reference pages in Notion ("Command guide", "How the pipeline works") are part of every change that
  alters a command, a skill, the process or a visible rule: edit `docs/notion/*.md` in the same commit and republish.
  The gate `scripts/hooks/guide_gate.py` (run with `--check` before each push, or registered by the owner as a
  PreToolUse hook on Bash) fails a push that changes `pipeline/`, `.claude/skills/` or `config/` without touching
  `docs/notion/` unless a commit message says `Guide: unchanged`; when `docs/notion/` changed it runs
  `python -m pipeline doc guide` first and holds the push if publishing fails.

## 5. State of the build (2026-09-27)

- **v2 code has landed, is tested offline and passed its first live `/setup` on 2026-09-27** (what the live run
  surfaced is in the decision log). Backblaze B2 storage backend
  (`pipeline/storage_b2.py`: native API, large files, retries, free-tier cap detection, id-named folders
  `raw/<domain id>/<primer id>/<stage id>/`), Notion backend (`pipeline/notion.py`: databases as the control
  panel, markdown-to-blocks, author page published in place into the author's row, comments read and replied),
  mode wiring from the environment, health check with a real bucket write test, rollover to the next account,
  video files stored as audio + key frames unless `--keep-full`.
- v1 (Google Drive / Sheets / Docs) code remains for the offline tests and is used only when no v2 variable is set.
- Not yet built: pictures inside pages (image upload is in the Notion client; the renderer does not emit images
  yet), per-card Notion pages and the Cards database (v2 publishes the whole author page), layer 3, transcription
  of stored audio.
- Owner's side: environment "AI Primer 2.0" exists with the Apify credential and the old Google keys. Done:
  Backblaze account, private bucket `aiprimer-rawfile-0001` (encryption on, object lock off) and public bucket
  `aiprimer-media-0001`. Pending: one application key (→ `B2_KEY_ID_0001` / `B2_APPLICATION_KEY_0001`),
  `B2_BUCKET_0001=aiprimer-rawfile-0001`, `B2_MEDIA_BUCKET_0001=aiprimer-media-0001` (all four variables are in
  the environment as of 2026-09-27); Notion workspace under the owner's personal account (the plan to use
  `ai.primer.brain.0001` died with that account's suspension), page "AI Primer", integration "AI Primer
  pipeline" connected to it (secret → API credential for `api.notion.com`; page id → `AIPRIMER_NOTION_PAGE_ID`).
  Done: allowed domains `*.backblazeb2.com`, `*.backblaze.com`, `api.notion.com`; setup script updated.
- **First live run (2026-09-27, `/setup`).** Notion page reachable; the seven databases (Accounts, Taxonomy, Folders,
  Resources, Authors, Summaries, Jobs) were created under "AI Primer" and later the same day moved into the layer
  pages by `setup layout`; 78 taxonomy nodes in 4 domains imported, each with its page under LAYER 3.
  `b2-0001`: bucket reachable, write test passed, 0 of 9.31 GB used, `raw/` and `raw/_Inbox/` created; media bucket
  reachable and public. Apify formats detected from the live actors: metadata actor takes `startUrls` (strings) and
  `maxItems`; transcript actor takes `urls` (strings). No resources and no authors yet.

## 6. Next engineering steps

1. Live smoke test, continued. `/setup` passed on 2026-09-27 (fixes in the decision log). Still to do: a temporary
   stage, one short public video, `/summarize`, open the Notion page, comment, republish, clean up.
2. First real example: one boxing coach, one jab video, one author page. Style check against the sample.
3. Pictures in pages (key frames and screenshots as image blocks) and the Cards database with one page per card.
4. Transcription of stored audio (video files dropped into the chat).
5. Layer 3: `/learn <stage>` dialogue that builds the Stage brief and its artefacts (Stages database).

## 7. Decision log

- 2026-09-19: repo `aiprimer2.0`, unrelated to the old `aiprimer`; cards in the repo are the source of truth;
  IDs permanent; folder names without numbers; Apify for YouTube metadata and transcripts; cost gates.
- 2026-09-19: all AI steps on the session model, including per-chunk extraction.
- 2026-09-20: logs extended: per resource (path, link, stored date, kind, extraction method, cost, warnings) and
  per summary (location, link, created, updated, version).
- 2026-09-20: Google service-account keys tried against shared Gmail folders; health check gained a real write
  test. Result superseded by the v2 decision.
- 2026-09-27: raw files move to Backblaze B2; summaries and logs move to Notion; Google is dropped.
- 2026-09-27: storage stays free via chained 10 GB Backblaze accounts; videos stored as audio + key frames by default.
- 2026-09-27: three-layer model confirmed; layer 3 built in dialogue; Cards is one library across authors.
- 2026-09-27: writing style fixed (conversational, newcomer-first, concise); sample `docs/style-sample-jab.md`.
- 2026-09-27: first live example wanted soon: one author, one resource, one Notion author page.
- 2026-09-27: v2 built: bucket folders named by node ids (renames free), Notion API pinned to 2022-06-28, video
  files stored as audio + key frames by default, storage accounts seeded from `B2_APPLICATION_KEY_<n>` variables.
- 2026-09-27: the media bucket is optional in the code (Backblaze requires a payment method for public buckets).
  The owner then chose to pay the one-time $1 fee and created `aiprimer-media-0001` (Public) next to the private
  raw bucket, after the agent advised against making the raw bucket itself public (originals would be exposed).
  Small pictures still go into Notion; the public bucket serves clips and large images.
- 2026-09-27: Notion runs under the owner's personal Google account (the dedicated Gmail was suspended by Google);
  no more throwaway Gmail accounts; plus-addresses for future Backblaze accounts.
- 2026-09-27: owner-facing documentation lives in Notion next to the databases: "Command guide" and "How the pipeline
  works", generated from `docs/notion/*.md` by `python -m pipeline doc guide` (idempotent, refreshed in place).
  The owner asked for a page to refer to; the agent keeps it current.
- 2026-09-27: the owner found the flat list of tables under "AI Primer" confusing and asked for a hierarchy by
  layer. Built: `LAYER 1 - RAW MATERIAL`, `LAYER 2 - SUMMARY BY AUTHORS`, `LAYER 3 - DOMAINS > PRIMERS > STAGES`
  (78 node pages), `LAYER 0 - CONFIG` last; the seven tables were moved into their layers live (rows copied and
  counted, old tables archived to Notion's trash). Stage pages are the future home of layer 3; `/taxonomy add` and
  `rename` keep the node pages in step. Earlier idea of a separate Stages database is superseded.
- 2026-09-27: the owner asked for the two reference pages inside `LAYER 0 - CONFIG`. Moved live through the API's
  move-page endpoint (sent with version 2026-03-11 for that one call; the pinned 2022-06-28 lacks it), links
  unchanged; `doc guide` now publishes there and moves a stray copy from the top. Pages can be moved by the API,
  databases cannot.
- 2026-09-27: the owner asked that the two reference pages be updated every time the pipeline changes, without
  fail. Decided: a gate, not a reminder (rule 11 of CLAUDE.md, script `scripts/hooks/guide_gate.py`). A push that
  changes the pipeline without the pages fails until the pages are updated or a commit states `Guide: unchanged`; a
  push that changes the pages republishes them first. The agent runs the gate before every push; registering it as
  a Claude Code hook (so it runs automatically) is a settings change the owner makes, see README → Setup.
- 2026-09-27: the first live `/setup` surfaced three defects, fixed the same day. (1) Backblaze answered the empty
  folder markers with an HTML 400: `requests` adds `Transfer-Encoding: chunked` to an empty file body next to
  `Content-Length: 0`; zero-byte uploads now go as bytes. (2) The environment installs `apify-client` 3.x, which
  returns pydantic models and renamed the arguments of `call()`; the runner was adapted and the requirement pinned to
  `apify-client>=3,<4`. (3) `setup fetch-apify-schemas` rewrote `config/pipeline.yaml` without its comments;
  `save_config` now round-trips through `ruamel.yaml` and keeps them. The live schema fetch corrected two actor
  field names (`maxItems`, transcript `urls`). The test suite now scrubs the live environment variables so that
  it passes inside the cloud environment too.

## 8. Open points

- Verify Backblaze's terms on multiple free accounts before relying on rollover.
- Confirm in the live test: Notion API file upload on the Free plan, YouTube embeds starting at a timestamp.
  (Settled 2026-09-27: the Backblaze key pair works from the environment variables `B2_KEY_ID_0001` /
  `B2_APPLICATION_KEY_0001`; Backblaze needs no entry in the API credentials box.)
- Choose the whiteboard tool (Excalidraw/tldraw vs Miro).
