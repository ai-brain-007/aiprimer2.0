# How the pipeline works

AI Primer turns the material you collect, such as videos, books, articles and screenshots, into verified, readable knowledge organised by author and by learning stage. This page explains the whole process once, in plain words. The Command guide page says what to type.

## In one paragraph

You drop resources into the chat. The system stores the originals, extracts their text and files each one under a stage of your Domain > Primer > Stage tree, with its author. When you ask for an author's summary, helper agents read everything by that author, pull out the ideas one by one and turn them into knowledge cards. Each card keeps a word-for-word quote that is checked against the source. The cards are rendered into one author page in Notion. Later, the learning layer will combine all authors' cards per stage into study material.

## Who does what

| Role | Who | What they do |
|---|---|---|
| Owner | You | Decide what goes in, where it belongs and what is good. Give feedback |
| Agent | The Claude session you talk to | Understands your request, proposes the details, runs the steps, asks when unsure |
| Helpers | Separate Claude workers started by the agent | One job each, with a fresh mind: extract ideas, match them with existing cards, review the result. A helper never checks its own work |
| Scripts | The pipeline programme | The mechanical work, done exactly the same way every time: upload, name, log, cut text into chunks, verify quotes, merge cards, render pages. Scripts never use AI |

```
you  -->  agent  -->  helpers (extract, match, review)
            |
            v
         scripts  -->  Backblaze (files)   Notion (tables and pages)   repository (cards)
```

## The three layers

| Layer | Holds | Where |
|---|---|---|
| 1. Raw | The originals, their extracted text and one row of details per resource | Files in Backblaze; the row in the Notion Resources table |
| 2. Per author | Knowledge cards, one idea each, with verified quotes; the author page rendered from them | Cards in the code repository; the page in the Notion Authors table |
| 3. Per stage | Study material for one stage built from all authors' cards: brief, procedures, drills, lists, scripts, glossary, self-test | The stage's page under LAYER 3 in Notion (the material itself is not built yet) |

Layer 3 only ever reads layer 2. It never goes back to the raw material. When something is missing, layer 2 is re-extracted for that gap.

## Step by step

### 1. Setup, once

Checks that the system can reach Notion and Backblaze, creates the layer pages and the seven tables under the "AI Primer" page, creates the storage folders, loads the tree of categories, builds the tree pages under LAYER 3 and checks the YouTube tools. Safe to repeat: it only adds what is missing.

### 2. Ingest, for every resource

1. You paste a link or drop a file.
2. The agent identifies it and proposes title, author, date and stage. You confirm.
3. The script stores the original in Backblaze in a folder named after the stage, extracts the text and stores that too. A transcript for a video, the text of a PDF, the table of a spreadsheet, the agent's reading of a screenshot. A video without captions is kept with its details only, and the row says so; transcribing the audio is a later phase.
4. A row is written to the Resources table: what it is, where it is, when it was published, how it was extracted, what it cost, and two clickable links: the resource itself (the YouTube video, or the stored original) and its stored text. Links to stored files last 7 days and are renewed by the pipeline.
5. Duplicates are recognised by a fingerprint of the content and skipped.

### 3. Taxonomy, when needed

The tree of Domain > Primer > Stage. Every node has a permanent id, so renaming costs nothing and moving a resource means updating its row and its storage folder. Each node also has its page under LAYER 3, renamed with it. Four domains today: Body, Mind, People, Money.

### 4. Summarize, per author

1. Plan: the script lists the author's resources not yet processed.
2. Extract: the text is cut into chunks. A helper reads each chunk and writes candidate cards: principles, concepts, techniques, drills, lists, scripts, examples, claims, mistakes, glossary terms. Each with a quote and its page or timestamp.
3. Verify: the script looks for every quote in the source text. No match, no card. Rejected cards are kept aside with the reason.
4. Match: a helper compares each new card with the author's existing cards. Same idea: merge. Evolved idea: a new dated version. New idea: a new card.
5. Review: a helper who only sees the evidence reads the result with fresh eyes and flags problems. For a big author the evidence is cut into parts and each part gets its own reviewer; the verdicts are merged. A card the reviewer faults for one unsupported sentence loses that sentence rather than the whole card; a card whose core is unsupported is rejected and kept aside with the reason.
6. Render and publish: the script writes the author page from the cards and publishes it into the author's row in the Authors table under LAYER 2. The link never changes. A version row goes into the Summaries table, with a clickable link to the page.

### 5. Feedback

You comment in Notion or in the chat. The agent applies the feedback to the cards and republishes. Pages are never edited by hand.

### 6. Learning layer, planned

For one stage, in dialogue with you: a brief, procedures, practice, lists, scripts, a glossary across authors and a self-test, all built from the cards. Agreement between independent authors weighs most, then how specific an author is, then how recent. Every sentence points to a card.

## How Notion is organised

Under the "AI Primer" page you find one page per layer. Open a layer page to reach its tables and pages. The sidebar shows the same tree, so you can jump straight to the layer or page you want.

| Page under AI Primer | What is inside |
|---|---|
| LAYER 1 - RAW MATERIAL | The Resources table: everything you added, with links to the stored originals |
| LAYER 2 - SUMMARY BY AUTHORS | The Authors table (one row per author, the author's page inside the row) and the Summaries table (version history) |
| LAYER 3 - DOMAINS > PRIMERS > STAGES | Your tree as pages: a page per domain, inside it a page per primer, inside that a page per stage. The stage pages will hold the study material |
| LAYER 0 - CONFIG | The pipeline's bookkeeping: Accounts, Taxonomy, Folders and Jobs, plus these two guides |

The tree pages are generated from the Taxonomy table. Adding or renaming a stage adds or renames its page; the page keeps its link. Nothing there is deleted automatically.

## Drawings

Drawings live on the pages they belong to, not on a board of their own.

- **Drawn by the pipeline, without you doing anything.** Every domain and primer page shows its part of the tree as a diagram, and every stage page shows where it sits. The diagrams are written as text that Notion draws, so they are redrawn whenever the tree changes. Later, stage pages will carry diagrams of procedures and flows made the same way.
- **Drawn by you.** For a free-hand sketch, open excalidraw.com, draw, and use Live collaboration or Save to get a link. Paste the link in the chat with the name of the page it belongs to, and it is pinned into that page as a board you can keep editing.

## Where things live

- Original files and extracted text: the private Backblaze bucket `aiprimer-rawfile-0001`. Free up to 10 GB; when full, the system moves to the next account.
- Pictures and clips shown inside pages: the public bucket `aiprimer-media-0001`. Originals never go there.
- Control panel: the seven tables, each inside its layer page under "AI Primer" in Notion.
- Knowledge cards: the code repository, folder `knowledge/<author>/units/`. This is the source of truth for every page.
- Author pages: the Authors table under LAYER 2, one row per author, the page inside the row.
- Stage pages: under LAYER 3, one page per stage; empty until the learning layer is built.

## The seven tables

| Table | Layer | One row per |
|---|---|---|
| Resources | LAYER 1 | Ingested resource: title, author, stage, dates, links, status, cost |
| Authors | LAYER 2 | Author, with the rendered page inside |
| Summaries | LAYER 2 | Published version of an author page |
| Accounts | LAYER 0 | Storage account, with its used and free space |
| Taxonomy | LAYER 0 | Node of the tree: domain, primer or stage, with the link to its page |
| Folders | LAYER 0 | Storage folder, mapped to its node |
| Jobs | LAYER 0 | Command that was run, with its result |

## Rules the system always follows

- Ids are permanent. Names can change; ids never do.
- Nothing is ingested twice unless you ask for it.
- Costs are shown before a channel is processed. Above 5 dollars you must confirm the amount.
- Every card has a quote the verifier found in the source. No quote, no card.
- Keys and passwords live only in the cloud environment, never in the chat, a file or a page.
- Storage stays free: a full account rolls over to the next one. Nothing is deleted.
- All AI work runs on the same model as your session. Helpers are never downgraded to a cheaper one.
- This page and the Command guide are regenerated from the repository whenever the pipeline changes. A change to the pipeline is not published without the two pages being checked and refreshed.

## Words used here

- Resource: one thing you added. A video, a PDF, a screenshot.
- Domain > Primer > Stage: your three-level tree. Body > Olympic Spartan > Boxing, for example.
- Card: one verified idea from one author, with its quote.
- Author page: the readable summary of one author, rendered from the cards.
- Inbox: the storage folder for files you upload by hand.
- Rollover: moving to the next storage account when one is full.
- Helper: a separate Claude worker with one job and no memory of the others.
