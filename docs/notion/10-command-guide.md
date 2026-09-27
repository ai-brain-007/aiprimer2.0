# Command guide

How you talk to the AI Primer system. Everything happens in a Claude Code chat session on the "AI Primer 2.0" environment. You type a command or simply say what you want. The agent asks for what it needs and does the rest.

## The four commands

| Command | When | What happens |
|---|---|---|
| `/setup` | Once at the start, or after you add a storage account or change the Notion page | Checks the connections, creates what is missing (layer pages, tables, tree pages, storage folders), prints a health table |
| `/ingest` | Every time you have a link or a file to add | Files the resource in storage and logs it in Notion |
| `/taxonomy` | When a category needs adding, renaming or moving | Updates the Domain > Primer > Stage tree |
| `/summarize <author>` | When you want an author's page built or refreshed | Builds the knowledge cards and publishes the author page |

You do not have to remember the exact words. "Add this video under Boxing" or "update Jack's page" works too.

## /ingest: adding a resource

1. Paste a YouTube link, or drop a file into the chat: PDF, Word, Excel, text, screenshot or video. Several at once is fine.
2. The agent reads what you gave it and proposes a title, an author, a date and a place in the tree (Domain > Primer > Stage).
3. Confirm or correct: "yes", "the author is Jack Dempsey", or "put it under Body > Olympic Spartan > Boxing".
4. The original goes into storage, the text is extracted, and a row appears in the Resources table in Notion.

Good to know:

- Something you already added is recognised and skipped. Say "re-ingest it" if you really want it again.
- A whole YouTube channel: paste the channel link. You first get the number of videos and the estimated cost, and nothing runs until you agree. Above 5 dollars you must confirm the amount.
- A screenshot or a scanned page: the agent reads it by eye and files the text it read.
- A video file dropped into the chat is stored as its audio track plus a few key frames, to keep storage small. Say "keep the full video" if you want the original kept.
- No date found: the agent asks for one. The date is what lets the system track how an author's ideas change over time.
- A YouTube video without captions is kept with its details only, and its row in the Resources table says why. Transcribing the audio comes in a later phase. A passing problem on YouTube's side, such as a sign-in check, is retried the next time the video is ingested.
- "Ingest the inbox": the agent picks up files you uploaded by hand into the `raw/_Inbox` folder of the storage bucket.

## /taxonomy: the tree of categories

- "Show the tree": lists every Domain > Primer > Stage.
- "Add a stage Clinch under Body > Olympic Spartan > Muay Thai": adds a node and its page under LAYER 3.
- "Rename Sales Man to Seller": renames the node and its page. Names are labels only; nothing moves in storage.
- "Move the footwork video to Boxing": moves one resource to another stage.
- "Sync the taxonomy": after you edited names directly in the Notion Taxonomy table, makes storage and the table consistent again.

## /summarize: building an author's page

1. Type `/summarize` and the author's name.
2. The agent shows the plan: which of the author's resources are new since the last run.
3. Helper agents extract the ideas, every quote is checked against the source, and a reviewer reads the result. Long sources take a while; you can leave the session open.
4. The author's page in Notion is created or refreshed: LAYER 2 - SUMMARY BY AUTHORS, table Authors, the author's row. The link never changes.
5. You read it and comment, in Notion or in the chat.

One author per session. Run it again after you ingest more of the author's material; only the new parts are processed.

## Giving feedback on a page

- In Notion: select a passage on the author's page and add a comment. In the next session, tell the agent to read the comments on that page.
- In the chat: "on Jack's page, the jab section is too long" works too.

The feedback is applied to the underlying cards, then the page is regenerated. Do not edit a page by hand: the next refresh overwrites it.

## Things never to do

- Never paste a key, token or password into the chat. If it happens, that key must be replaced.
- Never edit an author page by hand in Notion. Comment instead.
- Never delete files from the storage bucket to make room. When an account is full, the system moves on to the next one.

## When something looks wrong

- Say what you see, or paste a screenshot. The agent reads it.
- Every command ends with a short report in the chat, and the Jobs table in Notion keeps a log of what ran.
- `/setup` is safe to run at any time. It changes nothing that already exists.
