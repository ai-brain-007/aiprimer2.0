---
name: ingest
description: Ingest resources (YouTube videos or channels, PDFs, Word, Excel/CSV, text, screenshots, video files) into the AI Primer library - identify metadata, ask Domain > Primer > Stage and author, store in Google Drive, log in the control panel. Use when the user pastes links, attaches files, says /ingest, "ingest this", "add this resource", or mentions the inbox.
---

# /ingest — the ingestion agent

Inputs are whatever the user gave: URLs, files attached to the chat (use their local paths), or the word
`inbox` (files dropped into the `_Inbox` folder of the raw Drive). Optional overrides in the message:
`author=…`, `stage=…`, `title=…`, `date=…`.

Never run `ingest run` before the checklist below is answered, except when `stage=` (and `author=` if the
author cannot be detected) were given in the message.

## 1. Look before touching

```bash
python -m pipeline ingest probe <source…> --pretty
```

For each result:
- `status: duplicate` → tell the user it is already ingested (resource id, stage path, link) and skip it.
  Re-ingest only if the user explicitly says so (`--force`).
- `status: error` with a missing-file error → the attachment path is wrong; ask the user to re-attach.
- `status: channel` → go to section 4.
- `near_duplicates` non-empty → mention them (soft warning) but continue.
- `needs_vision: true` → note it: you will transcribe the image / scanned pages in step 5.

Read `metadata` (title, author_raw, published_date, date_precision, confidence, evidence). If confidence is
below 0.6, improve the guess yourself: for a PDF open the first pages (`Read` the file, or
`python -m pipeline ingest render-pages <pdf> --pages 1-3` and `Read` the PNGs); for an image `Read` it.
The publication date matters downstream (obsolescence, evolution): make a real effort to find it.

## 2. Several new resources?

Ask once whether they all belong to the same Domain > Primer > Stage (yes / decide per resource).

## 3. The checklist (AskUserQuestion, max 4 options per question)

Load the tree from the probe output (`taxonomy`). Ask, in one call when possible:

1. **Domain** — the 4 domains (`Body`, `Mind`, `People`, `Money`) plus, only if the user hinted at it, "New domain…".
2. **Primer** — the primers of that domain (≤3) plus "New primer…".
3. **Stage** — your 3 best guesses for this resource (use the content, the title and `stage_hints`; order by
   likelihood), plus "Show all stages…". If "Show all", ask again with the remaining stages 4 at a time,
   always with "New stage…" as the last option.
4. **Author** — "Detected: <author_raw>" (recommended) / each near match from `author_match.near_matches`
   ("Same as <canonical_name>") / "Other (type the name)". If `author_match.is_new` is false, the detected
   name already maps to an existing author: say so and offer to keep it.

Only ask for the **date** when `date_precision` is `unknown` or confidence is low: offer "Unknown" as an option;
accept any format (`2021`, `June 2021`, `2021-06-10`).

New domain / primer / stage → ask for the name (and one-line description), then:
```bash
python -m pipeline taxonomy add stage "<name>" --parent "<Domain / Primer>" --description "<…>"
```
(`add primer … --parent "<Domain>"`, `add domain "<name>"`). Folders are created on first ingestion.

## 4. Whole channels (or playlists)

```bash
python -m pipeline ingest channel <url> --list --pretty
```
Show: number of videos, already ingested, total hours, date range, estimated cost, and the confirmation
threshold. Ask the scope: **all pending** / **last 12 months** / **most recent N** / **date range**, and whether to
**skip Shorts**. Build the list of `video_id`s from the listing (respecting the scope), write it to
`.work/channel-select.json`, then:
- if `estimated_cost_usd` for the selection is above `cost_confirm_usd`, ask the user to confirm the amount
  explicitly (state it in dollars) before running;
- run the checklist (section 3) once for the whole channel (author = channel name unless told otherwise);
- ```bash
  python -m pipeline ingest channel <url> --select .work/channel-select.json --stage "<path>" --author "<name or A-id>" --pretty
  ```
Report ingested / skipped / failed counts; re-run the same command for failures (they resume).

## 5. Screenshots and scanned pages (needs_vision)

- **Image**: `Read` the image. Write a faithful markdown transcription following `pipeline/prompts/transcribe_page.md`
  into `.work/vision/<name>.md` (include any visible web address, author, date). Then run `ingest run` with
  `--extracted-file .work/vision/<name>.md` and `--title/--author/--date` from what you see or what the user says.
  Several screenshots of one page = one resource: ask, then concatenate the transcriptions into one file and
  ingest the FIRST image as the raw file with that combined text (mention the others in `--title`).
- **Scanned PDF**: run `ingest run` normally. If it returns `status: needs_vision`, `Read` the listed PNGs
  (only the low-confidence pages), write `{"<page>": "<markdown>"}` for those pages to `.work/vision/<id>.json`
  and re-run the same command with `--ocr-patch .work/vision/<id>.json`. Use `partial_text_file` to see what OCR got.

## 6. Do the work

```bash
python -m pipeline ingest run <source> --stage "<Domain / Primer / Stage>" --author "<name or A-id>" [--title "…"] [--date "…"] [--dataset-note "…"] --pretty
```
- `--author` accepts an existing id (`A-teddy-atlas`) or a name; a new name creates the author, a detected
  channel/author name different from the chosen author is recorded as an alias.
- Spreadsheets: ask "what does this dataset represent?" and pass it with `--dataset-note`.
- `status: retry` → an account was marked full; run the same command again (the next account is used).
- `status: needs_transcription` → a video/audio file was stored; transcription comes in a later phase.

Report a table: resource id · title · author · date · stage path · link, plus any warnings. End with:
"If a resource landed in the wrong stage: `/taxonomy move <R-id> to "<Domain / Primer / Stage>"`."

## Never

- paste or echo tokens; commit anything from `.cache/` or `.work/`; re-ingest a duplicate silently;
  ingest a channel without the cost line; invent a publication date (mark it unknown instead).
