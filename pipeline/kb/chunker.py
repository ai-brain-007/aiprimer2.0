"""Split the extracted text of a resource into chunks sized for helper agents.

Three source shapes are recognised:
- paged sources with ``<!-- page N -->`` markers (PDF, DOCX, scans): chapter-first using the
  table of contents when one is known, else windows of ``pages_per_window`` pages; any piece
  larger than ``chunk_tokens`` is split at heading / blank-line boundaries with about
  ``overlap_tokens`` of trailing context repeated at the start of the next chunk;
- transcripts with ``[mm:ss]`` / ``[h:mm:ss]`` markers: split at marker boundaries;
- plain text: split at blank lines (with overlap).

Tokens are estimated as characters / 4. Page and time markers stay inside the chunk text so
the extractor can cite locations. Every chunk is non-empty and indices are contiguous from 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PAGE_MARKER_RE = re.compile(r"^[ \t]*<!--\s*page\s+(\d+)\s*-->[ \t]*$", re.MULTILINE)
TIME_MARKER_RE = re.compile(r"\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]")
_HEADING_OR_MARKER_LINE_RE = re.compile(r"^(?:#{1,6} |[ \t]*<!--\s*page\s+\d+\s*-->)", re.MULTILINE)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n+")

CHUNK_TEXT_SENTINEL = "<!-- chunk text begins -->"
OVERLAP_START = "<!-- overlap: context repeated from the previous chunk; do not extract from it -->"
OVERLAP_END = "<!-- end of overlap -->"


@dataclass
class Chunk:
    chunk_id: str
    resource_id: str
    index: int
    text: str
    location_start: str = ""
    location_end: str = ""
    title: str = ""
    header: str = ""


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def chunk_id_for(resource_id: str, index: int) -> str:
    return f"{resource_id}#{index:02d}"


# --------------------------------------------------------------------------- packing


@dataclass
class _Block:
    text: str
    loc_start: str = ""
    loc_end: str = ""


@dataclass
class _Piece:
    text: str
    loc_start: str = ""
    loc_end: str = ""
    title: str = ""


def _hard_split(text: str, limit: int) -> tuple[str, str]:
    """Split text so that the head has at most `limit` characters, preferring whitespace."""
    if len(text) <= limit:
        return text, ""
    cut = -1
    for m in re.finditer(r"\n|\s", text[:limit]):
        if m.start() >= limit // 2:
            cut = m.start()
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(), text[cut:].lstrip()


def _trailing_overlap(blocks: list[_Block], overlap_chars: int, sep: str) -> str:
    if overlap_chars <= 0 or not blocks:
        return ""
    chosen: list[str] = []
    total = 0
    for b in reversed(blocks):
        cost = len(b.text) + (len(sep) if chosen else 0)
        if total + cost > overlap_chars:
            break
        chosen.insert(0, b.text)
        total += cost
    if chosen:
        return sep.join(chosen)
    tail = blocks[-1].text[-overlap_chars:]
    ws = re.search(r"\s", tail)
    if ws and ws.end() < len(tail):
        tail = tail[ws.end() :]
    return tail.strip()


def _overlap_cost(overlap_text: str) -> int:
    if not overlap_text:
        return 0
    return len(OVERLAP_START) + len(overlap_text) + len(OVERLAP_END) + 4


def _pack_blocks(blocks: list[_Block], chunk_chars: int, overlap_chars: int, sep: str = "\n\n") -> list[_Piece]:
    """Greedily pack consecutive blocks into pieces of at most chunk_chars characters."""
    pieces: list[_Piece] = []
    queue = [b for b in blocks if b.text.strip()]
    cur: list[_Block] = []
    cur_len = 0
    overlap_text = ""

    def flush() -> None:
        nonlocal cur, cur_len, overlap_text
        if not cur:
            return
        own = sep.join(b.text for b in cur)
        text = own
        if overlap_text:
            text = f"{OVERLAP_START}\n{overlap_text}\n{OVERLAP_END}\n\n{own}"
        pieces.append(_Piece(text=text, loc_start=cur[0].loc_start, loc_end=cur[-1].loc_end))
        overlap_text = _trailing_overlap(cur, overlap_chars, sep)
        cur = []
        cur_len = 0

    i = 0
    while i < len(queue):
        block = queue[i]
        budget = chunk_chars - _overlap_cost(overlap_text)
        if budget < max(chunk_chars // 2, 1):
            overlap_text = ""
            budget = chunk_chars
        add = len(block.text) + (len(sep) if cur else 0)
        if cur and cur_len + add > budget:
            flush()
            continue
        if not cur and len(block.text) > budget:
            head, tail = _hard_split(block.text, budget)
            if not head:
                head, tail = block.text[:budget], block.text[budget:]
            cur = [_Block(head, block.loc_start, block.loc_start)]
            cur_len = len(head)
            flush()
            if tail.strip():
                queue[i] = _Block(tail, block.loc_start, block.loc_end)
            else:
                i += 1
            continue
        cur.append(block)
        cur_len += add
        i += 1
    flush()
    return pieces


def _split_at_boundaries(text: str) -> list[str]:
    """Blank-line paragraphs, additionally split before heading and page-marker lines."""
    out: list[str] = []
    for para in _PARAGRAPH_SPLIT_RE.split(text):
        para = para.strip("\n")
        if not para.strip():
            continue
        starts = [m.start() for m in _HEADING_OR_MARKER_LINE_RE.finditer(para)]
        starts = [s for s in starts if s > 0]
        prev = 0
        for s in [*starts, len(para)]:
            piece = para[prev:s].strip("\n")
            if piece.strip():
                out.append(piece)
            prev = s
    return out


# --------------------------------------------------------------------------- paged sources


def _page_loc(page: int | None) -> str:
    return f"p. {page}" if page is not None else ""


def _split_pages(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Return (preamble before the first marker, [(page_no, segment including its marker)])."""
    matches = list(PAGE_MARKER_RE.finditer(text))
    preamble = text[: matches[0].start()] if matches else text
    pages: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages.append((int(m.group(1)), text[m.start() : end]))
    return preamble, pages


def _blocks_with_pages(text: str, page_before: int | None) -> list[_Block]:
    blocks: list[_Block] = []
    current = page_before
    for piece in _split_at_boundaries(text):
        markers = [int(m.group(1)) for m in PAGE_MARKER_RE.finditer(piece)]
        start = current
        if markers and PAGE_MARKER_RE.match(piece.lstrip("\n")):
            start = markers[0]
        end = markers[-1] if markers else current
        blocks.append(_Block(piece, _page_loc(start), _page_loc(end)))
        current = end
    return blocks


def _int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class _TocEntry:
    page: int
    order: int
    level: int
    title: str


def _parse_toc(toc: list[dict] | None) -> list[_TocEntry]:
    entries: list[_TocEntry] = []
    for i, e in enumerate(toc or []):
        if not isinstance(e, dict):
            continue
        page = None
        for key in ("page", "start_page", "page_start", "start", "page_number"):
            page = _int(e.get(key))
            if page is not None:
                break
        if page is None:
            continue
        level = _int(e.get("level")) or 1
        title = str(e.get("title") or e.get("name") or "").strip()
        entries.append(_TocEntry(page=page, order=i, level=level, title=title))
    entries.sort(key=lambda t: (t.page, t.order))
    return entries


def _window_ranges(page_numbers: list[int], pages_per_window: int) -> list[tuple[str, int, int, list[_TocEntry]]]:
    n = max(int(pages_per_window or 1), 1)
    distinct = sorted(set(page_numbers))
    ranges = []
    for i in range(0, len(distinct), n):
        group = distinct[i : i + n]
        ranges.append((f"Pages {group[0]}-{group[-1]}", group[0], group[-1], []))
    return ranges


def _toc_ranges(entries: list[_TocEntry], page_numbers: list[int]) -> list[tuple[str, int, int, list[_TocEntry]]]:
    first_page, last_page = min(page_numbers), max(page_numbers)
    top = min(e.level for e in entries)
    chapters = [e for e in entries if e.level == top]
    ranges: list[tuple[str, int, int, list[_TocEntry]]] = []
    if chapters[0].page > first_page:
        ranges.append(("Front matter", first_page, chapters[0].page - 1, []))
    for j, ch in enumerate(chapters):
        end = chapters[j + 1].page - 1 if j + 1 < len(chapters) else last_page
        if end < ch.page:
            continue  # another chapter starts on the same page; that one carries the pages
        subs = [e for e in entries if e.level == top + 1 and ch.page <= e.page <= end]
        ranges.append((ch.title or f"Pages {ch.page}-{end}", ch.page, end, subs))
    return ranges


def _sub_ranges(title: str, start: int, end: int, subs: list[_TocEntry]) -> list[tuple[str, int, int]]:
    if not subs:
        return [(title, start, end)]
    out: list[tuple[str, int, int]] = []
    if subs[0].page > start:
        out.append((title, start, subs[0].page - 1))
    for k, s in enumerate(subs):
        s_end = subs[k + 1].page - 1 if k + 1 < len(subs) else end
        if s_end < s.page:
            continue
        out.append((f"{title} > {s.title}" if s.title else title, s.page, s_end))
    return out


def _chunk_paged(
    text: str, toc: list[dict] | None, chunk_chars: int, overlap_chars: int, pages_per_window: int
) -> list[_Piece]:
    preamble, pages = _split_pages(text)
    if preamble.strip() and pages:
        pages[0] = (pages[0][0], preamble + pages[0][1])
    page_numbers = [p for p, _ in pages]
    entries = _parse_toc(toc)
    ranges = _toc_ranges(entries, page_numbers) if entries else _window_ranges(page_numbers, pages_per_window)

    pieces: list[_Piece] = []

    def emit(title: str, start: int, end: int) -> None:
        seg = [(p, t) for p, t in pages if start <= p <= end]
        if not seg:
            return
        seg_text = "".join(t for _, t in seg).strip("\n")
        if not seg_text.strip():
            return
        if len(seg_text) <= chunk_chars:
            pieces.append(_Piece(seg_text, _page_loc(seg[0][0]), _page_loc(seg[-1][0]), title))
            return
        blocks = _blocks_with_pages(seg_text, seg[0][0])
        for piece in _pack_blocks(blocks, chunk_chars, overlap_chars):
            piece.title = title
            pieces.append(piece)

    for title, start, end, subs in ranges:
        seg_len = sum(len(t) for p, t in pages if start <= p <= end)
        if seg_len <= chunk_chars or not subs:
            emit(title, start, end)
        else:
            for sub_title, s_start, s_end in _sub_ranges(title, start, end, subs):
                emit(sub_title, s_start, s_end)
    return pieces


# --------------------------------------------------------------------------- transcripts and plain text


def _chunk_transcript(text: str, chunk_chars: int) -> list[_Piece]:
    matches = list(TIME_MARKER_RE.finditer(text))
    blocks: list[_Block] = []
    preamble = text[: matches[0].start()]
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.start() : end]
        if i == 0 and preamble.strip():
            seg = preamble + seg
        blocks.append(_Block(seg, m.group(1), m.group(1)))
    pieces = _pack_blocks(blocks, chunk_chars, 0, sep="")
    for p in pieces:
        p.text = p.text.strip()
    return pieces


def _chunk_plain(text: str, chunk_chars: int, overlap_chars: int) -> list[_Piece]:
    blocks = [_Block(p) for p in _split_at_boundaries(text)]
    return _pack_blocks(blocks, chunk_chars, overlap_chars)


# --------------------------------------------------------------------------- public entry point


def _location_hint(kind: str) -> str:
    if kind == "paged":
        return "Locations: cite the page as `p. N`, taken from the nearest preceding `<!-- page N -->` marker."
    if kind == "transcript":
        return "Locations: cite the time as `mm:ss` (or `h:mm:ss`), taken from the nearest preceding `[mm:ss]` marker."
    return "Locations: this source has no page or time markers; leave the citation location empty."


def build_chunk_header(chunk: Chunk, resource: dict, total: int, kind: str) -> str:
    title = str(resource.get("title") or resource.get("resource_id") or "Untitled")
    published = resource.get("published_date") or "unknown"
    precision = resource.get("date_precision") or "unknown"
    stage = resource.get("stage_path") or resource.get("stage_id") or "unassigned"
    span = " - ".join(x for x in (chunk.location_start, chunk.location_end) if x) or "whole text"
    lines = [
        f"# {title}",
        "",
        f"Resource: {resource.get('resource_id', '')} | type: {resource.get('source_type', '') or 'unknown'} | "
        f"published: {published} (precision: {precision}) | stage: {stage}",
        f"Chunk: {chunk.chunk_id} ({chunk.index + 1} of {total}) | range: {span}"
        + (f" | section: {chunk.title}" if chunk.title else ""),
        _location_hint(kind),
        "",
        CHUNK_TEXT_SENTINEL,
    ]
    return "\n".join(lines)


def chunk_markdown(
    text: str,
    resource: dict,
    chunk_tokens: int,
    overlap_tokens: int,
    toc: list[dict] | None = None,
    pages_per_window: int = 30,
) -> list[Chunk]:
    if not text or not text.strip():
        return []
    chunk_chars = max(int(chunk_tokens), 1) * 4
    overlap_chars = max(int(overlap_tokens or 0), 0) * 4
    if overlap_chars * 2 > chunk_chars:
        overlap_chars = chunk_chars // 4
    resource_id = str(resource.get("resource_id", ""))

    if PAGE_MARKER_RE.search(text):
        kind = "paged"
        pieces = _chunk_paged(text, toc, chunk_chars, overlap_chars, pages_per_window)
    elif TIME_MARKER_RE.search(text):
        kind = "transcript"
        pieces = _chunk_transcript(text, chunk_chars)
    else:
        kind = "plain"
        pieces = _chunk_plain(text, chunk_chars, overlap_chars)

    chunks: list[Chunk] = []
    for piece in pieces:
        if not piece.text.strip():
            continue
        index = len(chunks)
        chunks.append(
            Chunk(
                chunk_id=chunk_id_for(resource_id, index),
                resource_id=resource_id,
                index=index,
                text=piece.text,
                location_start=piece.loc_start,
                location_end=piece.loc_end,
                title=piece.title,
            )
        )
    for chunk in chunks:
        chunk.header = build_chunk_header(chunk, resource, len(chunks), kind)
    return chunks
