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

The pipeline talks to Google with a **service account key**. A service account has no storage of its own
(Google removed it in 2023), so it can only create files inside a **Google Workspace Shared Drive** that it is a
member of. On a plain Gmail Drive it can read and edit files shared with it, but every upload and every new Doc
fails with a quota error. Plan for one Workspace Shared Drive for raw files and one for summaries (they can be the
same drive). If you only have Gmail accounts, use the fallback in "Gmail sign-in instead of a service account" below.

1. **Rotate any Apify token that was ever pasted into a chat.** Apify Console → Settings → Integrations.
2. **Google Cloud project and service account.** Create a project; enable the **Google Drive API**, **Google Sheets
   API** and **Google Docs API**. *IAM & Admin → Service accounts → Create service account* (any name, no roles
   needed) → *Keys → Add key → JSON*. Keep the downloaded file; you will paste its content into the environment in
   step 4. Note the service account's email (`…@….iam.gserviceaccount.com`).
3. **Shared Drives.** In Google Workspace, create a Shared Drive for raw files (e.g. "AI Primer Raw Files") and one
   for summaries (e.g. "AI Primer Summaries"), or one for both. In each, *Manage members* → add the service
   account's email as **Content manager**. Copy each drive's id from its address bar
   (`drive.google.com/drive/folders/<id>`).
4. **Cloud environment.** In Claude Code on the web, create an environment named **AI Primer**:
   - Network access **Custom**, allowed domain `api.apify.com`, tick *Also include default list of common package managers*.
   - **API credentials**: host `api.apify.com`, header `Authorization`, prefix `Bearer`, value = your new Apify token.
   - **Environment variables** (each key JSON on one line; quotes around the value are fine). One key per
     drive, or a single `GOOGLE_SERVICE_ACCOUNT_JSON` if the same service account serves both:
     ```
     GOOGLE_SERVICE_ACCOUNT_JSON_RAW={"type": "service_account", ...}
     GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY={"type": "service_account", ...}
     AIPRIMER_RAW_DRIVE_ID=<id of the raw Shared Drive>
     AIPRIMER_SUMMARY_DRIVE_ID=<id of the summaries Shared Drive>
     ```
   - **Setup script**: paste the contents of `scripts/setup-env.sh`.
5. **Control sheet.** Start a session in that environment and type `/setup`. The first run creates the sheet
   "AI Primer Control Panel" inside the summaries Shared Drive and prints its id; add it to the environment as
   `AIPRIMER_CONTROL_SHEET_ID`, start a new session and type `/setup` again: it creates the tabs, the folder tree
   `AI Primer Raw / Domain / Primer / Stage` and loads the taxonomy.
6. **Adding storage later:** create another Shared Drive, add the service account as Content manager, add a row in
   the Accounts tab (`account_id=raw02, role=raw, token_env_var=GOOGLE_SERVICE_ACCOUNT_JSON, drive_id=<id>,
   priority=2`), run `/setup`.

### Gmail sign-in instead of a service account (fallback)

Use this only for plain `@gmail.com` accounts, where a service account cannot upload. The pipeline then acts as
the Gmail account itself and uses its 15 GB.

- In the Google Cloud project: *OAuth consent screen* → External, add the Drive/Sheets/Docs scopes, publish to
  **In production** (in *Testing*, tokens expire after 7 days); *Credentials → OAuth client ID → Desktop app*;
  note the client ID and secret and add them to the environment as `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
- Authorise each Gmail once, either from the chat (`python -m pipeline auth url` → open the link signed in as the
  account, click Allow, paste back the `http://localhost…` address → `python -m pipeline auth exchange "<address>"`
  prints the refresh token) or on your computer (`python scripts/auth_local.py --client-id … --client-secret …`).
  Store the tokens as `GOOGLE_REFRESH_TOKEN_RAW01` and `GOOGLE_REFRESH_TOKEN_SUMMARY01`. Leave
  `GOOGLE_SERVICE_ACCOUNT_JSON` unset. Add a verified phone number to the accounts so they get 15 GB.

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
