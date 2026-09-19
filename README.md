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

1. **Rotate any Apify token that was ever pasted into a chat.** Apify Console → Settings → Integrations.
2. **Google Cloud project.** Create a project; enable the **Google Drive API**, **Google Sheets API** and
   **Google Docs API**. Open *APIs & Services → OAuth consent screen*: user type **External**, fill the app name and
   your email, add the scopes `…/auth/drive`, `…/auth/spreadsheets`, `…/auth/documents`, save, then set the
   publishing status to **In production** (in *Testing*, tokens expire after 7 days). Then *Credentials → Create
   credentials → OAuth client ID → Desktop app*. Note the **client ID** and **client secret**.
3. **Authorise each Gmail account** (on your own computer, once per account):
   ```bash
   pip install google-auth-oauthlib
   python scripts/auth_local.py --client-id <ID> --client-secret <SECRET>
   ```
   Sign in as `ai.primer.rawfile.0001@gmail.com`, click *Advanced → Go to … (unsafe)* on the "unverified app" page,
   click *Allow*. The script prints a refresh token: that is the value of `GOOGLE_REFRESH_TOKEN_RAW01`.
   Repeat signed in as `ai.primer.summary.0001@gmail.com` for `GOOGLE_REFRESH_TOKEN_SUMMARY01`.
   Add a verified phone number to both accounts so they get 15 GB instead of 5 GB.
4. **Cloud environment.** In Claude Code on the web, create an environment named **AI Primer**:
   - Network access **Custom**, allowed domain `api.apify.com`, tick *Also include default list of common package managers*.
   - **API credentials**: host `api.apify.com`, header `Authorization`, prefix `Bearer`, value = your new Apify token.
   - **Environment variables**:
     ```
     GOOGLE_OAUTH_CLIENT_ID=…
     GOOGLE_OAUTH_CLIENT_SECRET=…
     GOOGLE_REFRESH_TOKEN_RAW01=…
     GOOGLE_REFRESH_TOKEN_SUMMARY01=…
     ```
   - **Setup script**: paste the contents of `scripts/setup-env.sh`.
5. **Control sheet.** Start a session in that environment and type `/setup`. The first run creates the sheet
   "AI Primer Control Panel" and prints its id; add it to the environment as `AIPRIMER_CONTROL_SHEET_ID`, start a
   new session and type `/setup` again: it creates the tabs, the Drive folder tree and loads the taxonomy.
6. **Adding a second raw account later** (when the first Drive is full): repeat step 3 for the new Gmail, add
   `GOOGLE_REFRESH_TOKEN_RAW02` to the environment, add a row in the Accounts tab
   (`account_id=raw02, role=raw, token_env_var=GOOGLE_REFRESH_TOKEN_RAW02, priority=2`), run `/setup`.

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
