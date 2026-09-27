# AI Primer 2.0 — ingestion agent, summary agent, learning layer

A chat-driven knowledge pipeline that runs inside Claude Code cloud sessions. Files live in Backblaze B2, everything
you read lives in Notion, and the verified knowledge cards live in this repository.

- **Layer 1 — ingestion.** You drop a resource into the chat (a YouTube video or channel link, a PDF, a Word
  file, an Excel/CSV file, a screenshot, a video file). The agent finds the title, author and publication date,
  skips anything already ingested, asks which **Domain → Primer → Stage** it belongs to, stores the original and
  a text version in Backblaze, and logs one row in the Resources database in Notion.
- **Layer 2 — per author.** The agent extracts every principle, concept, technique, drill, exercise, list, script,
  example and glossary term into small "cards" (one file each, in this repository), removes duplicates, records
  how a technique changed between dated sources, fact-checks every card against the source text, and renders one
  Notion page per author with a stable link.
- **Layer 3 — per stage.** Built in dialogue with you from the cards of every author: a stage brief, SOPs, practice,
  lists, scripts, glossary crosswalk and self-test, each sentence citing a card. (Next phase.)

## How the data flows

```
you (chat) ── /ingest ──► ingestion agent ──► scripts ──► Backblaze bucket ai-primer-raw-0001: raw/<domain id>/<primer id>/<stage id>/
                                              │            original file + "<name>.extracted.md" (+ meta.json)
                                              └──────────► Notion · Resources database (+ Jobs log)
you (chat) ── /summarize <author> ──► summary agent ──► helper agents extract / match / review
                                                    ──► scripts verify quotes, merge cards, render
                                                    ──► knowledge/<author>/units/*.md (git)  +  Notion author page
```

| Data | Where it lives |
|---|---|
| Original files, text versions, audio and key frames | Backblaze bucket `ai-primer-raw-000N` (private), `raw / <domain id> / <primer id> / <stage id> /` |
| Files uploaded by hand for later | the same bucket, `raw / _Inbox /` |
| Pictures and clips embedded in pages | Uploaded into Notion (up to 5 MB each); YouTube embeds for video; an optional public bucket for larger media |
| Registry, taxonomy, storage accounts, authors, summaries, activity log | Notion databases under the page "AI Primer" |
| Knowledge cards (source of truth) | this repository, `knowledge/<author>/units/` |
| Readable summaries | the author's page in the Notion Authors database |

Credentials never live in this repository or in any page: they are settings of the Claude Code cloud environment.

## Setup (once, about an hour)

1. **Rotate any Apify token that was ever pasted into a chat.** Apify Console → Settings → Integrations.
2. **Backblaze**, signed in as the raw-files Gmail account: create the account, enable 2-Step Verification. Create
   one bucket, `ai-primer-raw-0001`, set to **Private**, encryption enabled, Object Lock disabled; in its
   Lifecycle Settings choose *Keep only the last version*. Bucket names are global across Backblaze; if the name
   is taken, choose another and remember it. Create one **application key** with read and write access to the
   account's buckets; note its **keyID** and **applicationKey**. The first 10 GB are free; a free account refuses
   uploads past that, and the pipeline then rolls over to the next account (see step 6).
   A second, **Public** bucket for pictures and clips shown inside pages is optional and Backblaze asks for a
   payment method to create one. Without it, pictures are uploaded into Notion (5 MB each) and videos are
   embedded from YouTube.
3. **Notion**, signed in as the "brain" Gmail account: create a workspace and keep it single-member (a second
   member turns the free plan into a capped trial; share pages to yourself as a guest instead). Create a top page
   named **AI Primer**. Under *Settings → Connections → Develop or manage integrations* create an internal
   integration **AI Primer pipeline** with read, update and insert content plus read and insert comments; copy its
   secret. Open the "AI Primer" page → ⋯ → *Connections* → add "AI Primer pipeline". Copy the page id: the 32
   characters at the end of the page address.
4. **Cloud environment.** In Claude Code on the web, click **+ New**, open the environment selector and create or
   edit **AI Primer 2.0**:
   - Network access **Custom**, allowed domains `api.apify.com`, `*.backblazeb2.com`, `*.backblaze.com`,
     `api.notion.com`; tick *Also include default list of common package managers*.
   - Environment variables:
     ```
     B2_KEY_ID_0001=<keyID>
     B2_APPLICATION_KEY_0001=<applicationKey>
     AIPRIMER_NOTION_PAGE_ID=<page id>
     ```
     plus `B2_BUCKET_0001=<name>` if the bucket is not called `ai-primer-raw-0001`, and `B2_MEDIA_BUCKET_0001=<name>`
     only if you created the optional public bucket.
   - Setup script: paste the contents of `scripts/setup-env.sh`. (The box does not start in the repository, must
     finish in about five minutes and must exit 0; the script is written for that.)
   - Save, reopen the environment (hover → settings icon) and add two **API credentials**: host `api.apify.com`,
     header `Authorization`, prefix `Bearer`, value = the new Apify token; host `api.notion.com`, header
     `Authorization`, prefix `Bearer`, value = the Notion integration secret.
5. **Bootstrap.** Start a session on the "AI Primer 2.0" environment, repository `aiprimer2.0`, and type `/setup`.
   It creates the databases under the "AI Primer" page, the `raw/` and `raw/_Inbox/` folders in the bucket, loads
   the taxonomy, runs a real write test into the bucket and prints a health table.
6. **Adding storage later.** Create Backblaze account 0002 (a new Gmail, its own bucket `…-0002`, its own
   key), add `B2_KEY_ID_0002` / `B2_APPLICATION_KEY_0002` to the environment, start a new session, run `/setup`.
   The row `b2-0002` appears in the Accounts database and receives uploads once `b2-0001` is full.

### Legacy: Google Drive / Sheets / Docs (v1)

The first version stored files in Google Drive and used a Google Sheet and Google Docs. That code is still in the
repository and covered by the offline tests, and it is used only when no v2 variable is set. Its setup is described
in `git log` history of this file; new installations should use Backblaze and Notion.

## Daily use

| You type | What happens |
|---|---|
| `/ingest` + a link or attached files | Probe, duplicate check, checklist (Domain → Primer → Stage → Author), storage, log. |
| `/ingest https://www.youtube.com/@channel/videos` | Lists the channel with count, hours and estimated Apify cost; asks the scope; ingests in batches. |
| `/ingest inbox` | Processes files uploaded into `raw/_Inbox/` of the raw bucket. |
| `/taxonomy rename "Rest Body" --name "Sleep"` | Renames the stage in the control panel and on the resources (storage folders are named by id and need no change). |
| `/taxonomy move R-YT-abc… to "Body / Immortal Yogi / Rest Body"` | Moves a mis-filed resource. |
| `/summarize Teddy Atlas` | Builds/updates the author's cards and rewrites the author's Notion page. |

The author pages are generated: do not edit them by hand (changes are overwritten). Leave comments on the page or
tell the agent in chat; the agent applies the feedback to the cards and republishes.

## Where to look when something goes wrong

- The **Resources** database answers "where is it?": readable path, bucket and object keys, links, when it was
  stored, what kind of resource it is, how its text was obtained, the Apify cost attributed to it, and any warnings.
- The **Authors** database answers "where is the summary?": the row is the page; its properties hold creation date,
  last update, version, and the link to the cards on GitHub. The **Summaries** database keeps one row per version.
- The **Accounts** database shows each Backblaze account, its buckets, used space and status.
- `python -m pipeline auth check --pretty` — access, write test and used space per account.
- `python -m pipeline setup status --pretty` — control panel link, taxonomy size, resources by status.
- The **Jobs** database — every command, its result and its Apify cost.
- A resource stuck in `registered` / `folder_ready` / `uploaded` resumes when the same `/ingest` is run again.

## Development

```bash
pip install -r requirements.txt
python -m pytest -q          # all tests run against in-memory fakes; no network, no credentials
python -m pipeline --help
```

Layout: `pipeline/` (scripts: storage and Notion clients, registry, taxonomy, ingest, knowledge base),
`.claude/skills/` (the agent instructions for `/setup`, `/ingest`, `/taxonomy`, `/summarize`), `pipeline/prompts/`
(helper-agent briefs and the writing style), `config/` (settings and the taxonomy seed), `knowledge/` (cards and
rendered summaries), `docs/` (working agreement, style sample), `tests/`. See `CLAUDE.md` for the rules the agent
follows and `docs/working-agreement.md` for the decisions.
