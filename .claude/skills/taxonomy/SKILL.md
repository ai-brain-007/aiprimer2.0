---
name: taxonomy
description: Manage the Domain > Primer > Stage tree of the AI Primer library (list, add, rename, sync storage folders with the control panel) and move a mis-filed resource to another stage. Use when the user says /taxonomy, wants to rename a domain/primer/stage, edited names in the Taxonomy database, or says a resource is in the wrong place.
---

# /taxonomy — tree and moves

The **Taxonomy database** of the control panel is the source of truth. Node ids (`T-…`) never change; names do.
In Backblaze storage the folders are named by node id (`raw/<domain id>/<primer id>/<stage id>/`), so a rename touches no file: only the
readable `stage_path` on the resources is refreshed. (In the legacy Google mode the Drive folders carry the names
and are renamed.) Every command prints JSON.

## list
```bash
python -m pipeline taxonomy list --pretty
```
Show the tree as an indented list with node ids only when useful.

## add
```bash
python -m pipeline taxonomy add domain "<name>" --description "<…>"
python -m pipeline taxonomy add primer "<name>" --parent "<Domain>" --description "<…>"
python -m pipeline taxonomy add stage "<name>" --parent "<Domain / Primer>" --description "<…>" [--create-folders]
```
Names are plain (no numbers). Folders are created on first ingestion unless `--create-folders` is given.

## rename
1. Check the blast radius first and tell the user how many resources and which accounts' folders are affected:
   ```bash
   python -m pipeline taxonomy affected "<node id or path>" --pretty
   ```
2. Confirm, then:
   ```bash
   python -m pipeline taxonomy rename "<node id or path>" --name "<new name>" --pretty
   ```
   This updates the sheet (name, slug, path, previous_names), renames the Drive folder in every account that has
   one, and refreshes `stage_path` on the affected resources. Report the JSON `errors` list if non-empty and run
   `sync` to finish.

## sync (after the user edited names directly in the Taxonomy tab, or after an error)
```bash
python -m pipeline taxonomy sync --pretty            # rename folders to match the sheet, report orphans
python -m pipeline taxonomy sync --create-missing    # also create every missing folder in the first raw account
```
Orphans (folders in Drive that no node references) are only reported, never deleted: list them for the user.

## move a resource
```bash
python -m pipeline resource move "<R-id or a distinctive part of the title>" --stage "<Domain / Primer / Stage>" --pretty
```
If the title fragment matches several resources the command lists them: ask the user which one. If the target
stage does not exist, offer to create it (see add). The original file, the text version and any CSV exports move
together (in Backblaze a move is a server-side copy plus delete, so the object keys change) and the Resources row
is updated with the new keys and links.

## correct a resource's metadata
```bash
python -m pipeline resource set "<R-id>" [--title "…"] [--author "<name or A-id>"] [--date "…"] [--alias-of A-…] --pretty
```
`--alias-of` records the given author name as an alias of an existing author (e.g. a channel name for a person).
Drive file names are updated to match.
