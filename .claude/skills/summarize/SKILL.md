---
name: summarize
description: Build or update an author's knowledge base (deduplicated principles, concepts, techniques, drills, lists, scripts, examples, glossary; with evolution tracking and two fact-check gates) and refresh the author's summary Google Doc. Use when the user says /summarize, asks for a summary of an author, or asks to update summaries after ingesting.
---

# /summarize <author> — the summary agent

`<author>` is an `A-…` id or a name (resolve it with `python -m pipeline resource list --pretty` if needed).
One author per session. Work in `.work/<author-id>/`. Every CLI command prints JSON; read the `next` field.

## 1. Plan
```bash
python -m pipeline summarize plan --author <A-id> --pretty
```
Shows the author's resources not yet in the knowledge base with size estimates and a proposed batch. If more than
10 resources are pending, ask the user: proposed batch / all / choose. Warn that a 400-page book means ~20 helper runs.

## 2. Prepare
```bash
python -m pipeline summarize prepare --author <A-id> --resources R-…,R-… --pretty
```
Downloads the text versions and writes `chunks/NN.md` per resource plus `index.json` (existing cards).

## Model rule

Every AI step (identification, checklist, extraction, matching decisions, review, vision transcription) runs on
the model of this chat session. Helper agents are the same model with a fresh memory: never pass a `model`
override when spawning them. The user decided this; cost is not a reason to change it.

## 3. Extract (helper agents, fresh context, up to `kb.parallel_helpers` in parallel)
For every chunk file listed in the prepare output, spawn a helper (Agent tool, subagent_type `general-purpose`)
whose prompt is `pipeline/prompts/extract_units.md` with the placeholders filled (`{{chunk_path}}`,
`{{output_path}}`, `{{resource_id}}`, `{{chunk_id}}`). The helper reads the chunk and writes `units-NN.json`.
When a resource has more than one chunk, spawn one consolidation helper with
`pipeline/prompts/consolidate_resource.md` (placeholders `{{input_paths}}` = the chunk output files,
`{{output_path}}` = `consolidated_path`, `{{resource_id}}`) that reads all chunk outputs and writes the consolidated file.
Do not extract cards yourself in the main context: your job is orchestration and the conversation.

## 4. Fact-check gate 1 (mechanical)
```bash
python -m pipeline summarize verify --author <A-id> --resource R-… --pretty
```
Every citation quote must be found in the source. Report the number of verified units and rejects (rejects are
kept in `_rejected/` with the reason). A high reject rate (> 30%) means the helper paraphrased quotes: re-run the
extraction for that resource with the reminder "quotes must be copied verbatim".

## 5. Match (script + one helper per resource)
```bash
python -m pipeline summarize match --author <A-id> --resource R-… --pretty
```
Writes the candidates file. If `needs_helper` is above zero, spawn a helper with `pipeline/prompts/match_units.md`
(placeholders `{{candidates_path}}`, `{{output_path}}` = the `decisions_path` from the command output, `{{resource_id}}`)
that writes the decisions file with NEW / SAME / EVOLVED / CONTRADICTS per card. Cards the script decided
automatically (`auto_new`, `auto_same`) need no helper decision.

## 6. Apply and review (gate 2)
```bash
python -m pipeline summarize apply --author <A-id> --resource R-… --pretty      # for each resource
python -m pipeline summarize review-prep --author <A-id> --pretty               # writes evidence.md
```
Spawn ONE reviewer helper with `pipeline/prompts/review_units.md` (placeholders `{{author_id}}`, `{{evidence_path}}`,
`{{output_path}}` = the `review_path` from the command output). It must not see anything else. It writes the review file.

## 7. Confirm with the user
Show: cards new / same / evolved / contradicted / rejected, the contradictions found, the reviewer's verdict.
Check the Doc's comments first if a Doc exists: `python -m pipeline doc comments --author <A-id> --pretty`;
offer to apply them to the cards (edit the unit files directly, then `doc comments --resolve <id>`).
Ask to finalise.

## 8. Finalise
```bash
python -m pipeline summarize finalize --author <A-id> --note "<what this version adds>" --pretty
```
Applies the review verdicts (rejected → `_rejected/`), renders `summary.md`, creates or refreshes the Google Doc
in place (same link), commits `knowledge/<author-slug>/`, logs the Summaries row, marks resources summarised.
Report the Doc link, the version and the commit. Running the whole flow again for the same resources adds nothing.

## Rules
- Never write a card without a verbatim quote; never edit `summary.md` by hand (it is regenerated).
- EVOLVED needs dated sources on both sides; when unsure prefer SAME over NEW, and NEW over EVOLVED.
- Contradictions are flagged, never silently resolved: the Doc lists them for the user.
- Commit only `knowledge/<author-slug>/**`.
