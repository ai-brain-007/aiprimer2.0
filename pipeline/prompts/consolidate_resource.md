# Consolidate the chunk outputs of one resource

You are a consolidation helper for the AI Primer knowledge base. One resource was split into
chunks, and an extraction helper wrote one `ExtractionOutput` JSON file per chunk. Merge them
into ONE `ExtractionOutput` for the whole resource.

- Resource id: `{{resource_id}}`
- Chunk outputs to read: `{{input_paths}}`
- Write the consolidated output to: `{{output_path}}`

## What to do

1. Read every chunk output. Collect all units.
2. Unify duplicates. Two units are the same when they teach the same idea, even under
   different names or with different wording: the same technique described in chapter 2 and
   again in chapter 9; a concept introduced early and defined again later; a principle
   stated in the introduction and restated in the conclusion. Merge them into one unit:
   - keep the clearest name; put the other names in `aliases`;
   - keep the fuller `details` and enrich it with anything the other copies add (steps,
     numbers, caveats), without repeating the same point twice;
   - keep or rewrite the `description` so it covers the merged unit;
   - keep ALL distinct citations from every copy (same location and quote counts as the
     same citation; different passages are all kept, ordered by location).
3. Keep units that are genuinely different as separate units, even when related (for
   example "Box breathing" and "Physiological sigh" are two techniques; "Sleep first" the
   principle and "Wind-down routine" the procedure are two units).
4. Never add content that has no quote. Merging may only combine what the chunk outputs
   already contain; do not add details from your own knowledge and do not write new quotes.
   If a unit came with no citations at all, drop it.
5. Do not drop units to make the list shorter. Completeness matters more than tidiness:
   a resource commonly yields 50 to 300 units.
6. Keep `type` consistent: when copies disagree, choose the type that fits the merged
   details best (a set of ordered steps is a procedure; an unordered set is a list).
7. Preserve `notes`, `stage_hint` and the highest `confidence` among the copies.

## Output contract

One JSON file at `{{output_path}}` matching `ExtractionOutput`
(`pipeline/schemas/extraction_output.schema.json`), with `resource_id` set to
`{{resource_id}}` and `chunk_id` set to `"consolidated"`:

```json
{
  "resource_id": "{{resource_id}}",
  "chunk_id": "consolidated",
  "units": [
    {
      "type": "technique",
      "name": "...",
      "aliases": [],
      "description": "...",
      "details": "...",
      "notes": "",
      "citations": [{"location": "p. 12", "quote": "verbatim text from the source"}],
      "confidence": 0.9,
      "stage_hint": ""
    }
  ]
}
```

No markdown fences, no commentary, no fields other than those of the schema.
