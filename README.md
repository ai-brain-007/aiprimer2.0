# AI Primer 2.0 — ingestion agent and summary agent

A chat-driven knowledge pipeline that runs inside Claude Code cloud sessions.

- **Layer 1 — ingestion.** You drop a resource into the chat (a YouTube video or channel link, a PDF, a Word
  file, an Excel/CSV file, a screenshot, a video file). The agent finds the title, author and publication date,
  skips anything already ingested, asks which **Domain → Primer → Stage** it belongs to, stores the original and
  a text version in the raw-file Google Drive, and logs one row in the control-panel spreadsheet.
- **Layer 2 — summaries per author.** The agent extracts every principle, concept, technique, drill, exercise,
  list, script, example and glossary term into small "cards" (one file each, in this repository), removes
  duplicates, records how a technique changed between dated sources, fact-checks every card against the source
  text, and renders one Google Doc per author with a stable link.

## How the data flows

```
you (chat) ── /ingest ──► ingestion agent ──► scripts ──► raw Drive: AI Primer Raw/<Domain>/<Primer>/<Stage>/
                                              │            original file + "<name>.extracted.md"
                                              └──────────► control panel sheet: Resources row (+ Jobs log)
you (chat) ── /summarize <author> ──► summary agent ──► helper agents extract / match / review
                                                    ──► scripts verify quotes, merge cards, render
                                                    ──► knowledge/<author>/units/*.md (git)  +  Google Doc "Summary — <Author>"
```

| Data | Where it lives |
|---|---|
| Original files and text versions | Drive of the raw-file Gmail account, `AI Primer Raw / Domain / Primer / Stage /` |
| Files dropped from a phone/laptop for later | `AI Primer Raw / _Inbox` |
| Registry, taxonomy, accounts, authors, summaries, activity log | Google Sheet "AI Primer Control Panel" in the summary Gmail account |
| Knowledge cards (source of truth) | this repository, `knowledge/<author>/units/` |
| Readable summaries | Google Docs in `AI Primer Summaries /` of the summary Gmail account |

Credentials never live in this repository or in any sheet: they are settings of the Claude Code cloud environment.

## Setup (once, about an hour)

The pipeline talks to Google **as your two Gmail accounts** (`ai.primer.rawfile.0001` for files,
`ai.primer.summary.0001` for the control sheet and the summary Docs). Each account authorises the pipeline once by
clicking *Allow*; the resulting key is stored on the Claude Code cloud environment. Files are then owned by the
Gmail account and use its 15 GB. (A Google *service account* cannot do this on Gmail: Google gives service accounts
no storage, so they can only write inside a paid Workspace Shared Drive. See "Alternative" below.)

1. **Rotate any Apify token that was ever pasted into a chat.** Apify Console → Settings → Integrations.
2. **Google Cloud project.** Create a project (or reuse one); enable the **Google Drive API**, **Google Sheets API**
   and **Google Docs API**. Open *APIs & Services → OAuth consent screen*: user type **External**, app name
   "AI Primer", your email as contact; add the scopes `…/auth/drive`, `…/auth/spreadsheets`, `…/auth/documents`;
   save; then **Publish app** so the status reads **In production** (in *Testing*, keys expire after 7 days).
   Then *Credentials → Create credentials → OAuth client ID → Desktop app*. Note the **client ID** and **client secret**.
3. **Cloud environment.** In Claude Code on the web, click **+ New**, open the environment selector (it says
   "Default") and choose **Add cloud environment**:
   - Name `AI Primer`.
   - Network access **Custom**, allowed domain `api.apify.com`, tick *Also include default list of common package managers*.
   - Environment variables:
     ```
     GOOGLE_OAUTH_CLIENT_ID=<client id>
     GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
     ```
   - Setup script: paste the contents of `scripts/setup-env.sh`.
   - Create it, then reopen it (hover → settings icon) and add an **API credential**: host `api.apify.com`, header
     `Authorization`, prefix `Bearer`, value = the new Apify token.
4. **Authorise the two Gmail accounts** (once each). Start a session on the "AI Primer" environment, repository
   `aiprimer2.0`, and type `/setup`. The agent prints a link. Open it signed in as `ai.primer.rawfile.0001@gmail.com`,
   click *Advanced → Go to AI Primer (unsafe)* on the "unverified app" page, click *Allow*. The browser lands on an
   `http://localhost…` page that fails to load: copy that page's full address and paste it into the chat. The agent
   returns the key and its variable name (`GOOGLE_REFRESH_TOKEN_RAW01`); add it to the environment variables.
   Repeat signed in as `ai.primer.summary.0001@gmail.com` for `GOOGLE_REFRESH_TOKEN_SUMMARY01`.
   Add a verified phone number to both accounts so they get 15 GB rather than 5 GB.
5. **Control sheet.** Start a new session and type `/setup`: it creates the sheet "AI Primer Control Panel" in the
   summary account and prints its id. Add it as `AIPRIMER_CONTROL_SHEET_ID`, start a new session, `/setup` once more:
   it creates the tabs, the folder tree `AI Primer Raw / Domain / Primer / Stage` and loads the taxonomy.
6. **Adding a second raw account later** (when the first Drive is full): authorise the new Gmail as in step 4, store
   its key as `GOOGLE_REFRESH_TOKEN_RAW02`, add a row in the Accounts tab
   (`account_id=raw02, role=raw, token_env_var=GOOGLE_REFRESH_TOKEN_RAW02, priority=2`), run `/setup`.

### Alternative: service account with Google Workspace Shared Drives

If you have (or buy) Google Workspace, a service-account key can replace the sign-in: create two Shared Drives, add
the service account's email as **Content manager** of each, and set `GOOGLE_SERVICE_ACCOUNT_JSON` (or one key per
drive: `GOOGLE_SERVICE_ACCOUNT_JSON_RAW` / `GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY`) plus `AIPRIMER_RAW_DRIVE_ID` /
`AIPRIMER_SUMMARY_DRIVE_ID` on the environment. Leave the refresh-token variables unset. On plain Gmail accounts this
mode can edit a shared sheet but every upload is refused by Google.

## Daily use

| You type | What happens |
|---|---|
| `/ingest` + a link or attached files | Probe, duplicate check, checklist (Domain → Primer → Stage → Author), storage, log. |
| `/ingest https://www.youtube.com/@channel/videos` | Lists the channel with count, hours and estimated Apify cost; asks the scope; ingests in batches. |
| `/ingest inbox` | Processes files dropped into `AI Primer Raw / _Inbox`. |
| `/taxonomy rename "Rest Body" --name "Sleep"` | Renames the stage in the sheet, the Drive folders and the resources' paths. |
| `/taxonomy move R-YT-abc… to "Body / Immortal Yogi / Rest Body"` | Moves a mis-filed resource. |
| `/taxonomy sync` | After editing names in the Taxonomy tab: renames Drive folders to match. |
| `/summarize Teddy Atlas` | Builds/updates the author's cards and refreshes the summary Doc. |

The summary Docs are generated: do not edit them by hand (changes are overwritten). Leave comments on the Doc
or tell the agent in chat; the agent applies the feedback to the cards and re-renders.

## Where to look when something goes wrong

- The **Resources** tab answers "where is it?": Drive path, folder and file links, when it was stored, what kind of
  resource it is, how its text was obtained, the Apify cost attributed to it, and any quality warnings.
- The **Authors** tab answers "where is the summary?": Doc link, creation date, last update, version, and the link to
  the cards on GitHub. The **Summaries** tab keeps one row per version.
- `python -m pipeline auth check --pretty` — credentials and free space per account.
- `python -m pipeline setup status --pretty` — control sheet link, taxonomy size, resources by status.
- The **Jobs** tab — every command, its result and its Apify cost.
- A resource stuck in `registered` / `folder_ready` / `uploaded` resumes when the same `/ingest` is run again.

## Development

```bash
pip install -r requirements.txt
python -m pytest -q          # all tests run against in-memory fakes; no network, no credentials
python -m pipeline --help
```

Layout: `pipeline/` (scripts: Google clients, registry, taxonomy, ingest, knowledge base), `.claude/skills/`
(the agent instructions for `/setup`, `/ingest`, `/taxonomy`, `/summarize`), `pipeline/prompts/` (helper-agent
briefs), `config/` (settings and the taxonomy seed), `knowledge/` (cards and rendered summaries), `tests/`.
See `CLAUDE.md` for the rules the agent follows.
