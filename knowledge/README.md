# knowledge/

The per-author knowledge base: what each author teaches, as verified "cards", plus the
rendered summary. This directory is the source of truth for the summaries; the Google Docs
are generated from it and are overwritten on every run.

## Layout

```
knowledge/
  README.md
  <author-slug>/                 author_id without the "A-" prefix, e.g. A-teddy-atlas -> teddy-atlas
    author.yaml                  author_id, canonical_name, aliases, type, summary_doc_id, summary_doc_url, version
    units/U-xxxxxx.md            one card per file, named by the unit id
    _rejected/<ts>-<temp_id>.md  audit trail of extractions that failed verification or review
    summary.md                   the rendered summary (and "summary - <Domain>.md" parts when split)
```

## Card files (`units/U-xxxxxx.md`)

YAML front matter with the card's metadata, then a markdown body with four sections. The
file round-trips losslessly through `pipeline.kb.units.parse_unit_file` /
`render_unit_file`; edit with care and keep the section headings.

```
---
id: U-abc123
author_id: A-teddy-atlas
type: technique              # one of the 13 unit types (see below)
name: Box breathing
aliases: [Square breathing]
stages: [T-xxxxxx]           # taxonomy stage ids; the first one is where the card is filed
status: active               # active | contradicted | superseded | needs_review
first_seen: '2018-06-01'     # dates of the sources; "YYYY", "YYYY-MM" or "YYYY-MM-DD"
last_seen: '2023-05-01'
citations:                   # verified quotes; resource_id + location (p. N or mm:ss)
  - {resource_id: R-F-abc, location: p. 42, quote: "...", verified: true, score: 100}
versions:                    # one entry per dated source that taught this card; sorted by date
  - {date: '2018-06-01', resource_id: R-F-abc, summary: '...', what_changed: ''}
contradictions: []
created_at: ...
updated_at: ...
---

## Description
One to three sentences in the pipeline's words.

## Details
The full teaching as the sources give it.

## Notes
Caveats and context.

## Earlier version
(only present after an EVOLVED merge) the previous description and details, prefixed with their date
```

Unit types: principle, concept, technique, drill, exercise, list, script, example, glossary,
procedure, claim, mistake, dataset.

## Conventions

- Every card has at least one citation whose quote was found in the source text
  (`pipeline.kb.verify`). Cards without a verified quote are never written; they go to
  `_rejected/` with the reason.
- Ids are never renamed or reused. A card that turns out to be a duplicate is merged into
  the older one and deleted; its id may appear in old changelogs.
- Dates are strings and keep the precision of the source (`2019`, `2019-05`, `2019-05-12`);
  an unknown date is an empty string. Evolution tracking needs dated sources.
- `stages` come from the resource's stage at ingest time (or the summary agent's
  assignment); the summary groups cards by the first stage.
- `_rejected/` files are kept for audit and are safe to delete once reviewed.
- `summary.md` is regenerated on every run; never edit it by hand.
- Commit the whole author directory together with the run that produced it.
