"""Knowledge-base layer: per-author knowledge units ("cards").

Modules:
- units:     unit card files (front matter + markdown body), the UnitStore and author metadata
- chunker:   split extracted source text into helper-sized chunks
- verify:    check that every citation quote really appears in the source
- matcher:   find existing cards that a freshly extracted unit may duplicate or evolve
- merge:     apply NEW / SAME / EVOLVED / CONTRADICTS decisions to the store
- render:    render the author summary markdown from the store
- workspace: file layout of the per-author working directory
- evidence:  markdown evidence pack for the independent reviewer
- schemas:   JSON schemas of the helper-agent contracts
"""
