import re

from pipeline.kb.chunker import CHUNK_TEXT_SENTINEL, OVERLAP_END, OVERLAP_START, chunk_markdown, estimate_tokens

RESOURCE = {
    "resource_id": "R-F-abc",
    "title": "The Book",
    "published_date": "2020-03",
    "date_precision": "month",
    "stage_id": "T-1",
    "stage_path": "Body > Immortal Yogi > Rest Body",
    "source_type": "pdf",
    "source_url": "",
    "raw_file_url": "https://drive.example/x",
}


def paged_text(pages: int, words_per_page: int = 120) -> str:
    parts = []
    for p in range(1, pages + 1):
        body = " ".join(f"w{p}_{i}" for i in range(words_per_page))
        parts.append(f"<!-- page {p} -->\n\nParagraph one of page {p}. {body}\n\nParagraph two of page {p}.\n\n")
    return "".join(parts)


def own_text(chunk_text: str) -> str:
    if OVERLAP_END in chunk_text:
        return chunk_text.split(OVERLAP_END, 1)[1]
    return chunk_text


def check_common(chunks, chunk_tokens):
    assert chunks, "no chunks produced"
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.text.strip()
        assert c.chunk_id == f"{RESOURCE['resource_id']}#{c.index:02d}"
        assert c.resource_id == RESOURCE["resource_id"]
        assert estimate_tokens(c.text) <= chunk_tokens, f"chunk {c.index} too large"
        assert c.header and CHUNK_TEXT_SENTINEL in c.header
        assert c.header not in c.text


def test_toc_based_chunking():
    text = paged_text(60)
    toc = [
        {"title": "Intro", "level": 1, "page": 2},
        {"title": "Sleep", "level": 1, "page": 20},
        {"title": "Light", "level": 2, "page": 26},
        {"title": "Temperature", "level": 2, "page": 33},
        {"title": "Food", "level": 1, "page": 45},
    ]
    chunk_tokens = 2500  # ~10 pages of ~1000 chars each
    chunks = chunk_markdown(text, RESOURCE, chunk_tokens, 100, toc=toc, pages_per_window=30)
    check_common(chunks, chunk_tokens)
    # the first chunk is the front matter before the first chapter
    assert chunks[0].location_start == "p. 1"
    assert chunks[0].title == "Front matter"
    titles = {c.title for c in chunks}
    assert "Intro" in titles and "Food" in titles
    assert any(t.startswith("Sleep > Light") for t in titles)
    assert any(t.startswith("Sleep > Temperature") for t in titles)
    # every page is covered exactly once by the "own" (non-overlap) text
    seen = []
    for c in chunks:
        seen += [int(p) for p in re.findall(r"<!-- page (\d+) -->", own_text(c.text))]
    assert seen == list(range(1, 61))
    # locations are page markers and monotonic
    for c in chunks:
        assert re.fullmatch(r"p\. \d+", c.location_start) and re.fullmatch(r"p\. \d+", c.location_end)
    assert chunks[-1].location_end == "p. 60"
    # overlap context is repeated from the previous chunk
    assert any(OVERLAP_START in c.text for c in chunks[1:])


def test_no_toc_uses_page_windows():
    text = paged_text(100, words_per_page=30)
    chunks = chunk_markdown(text, RESOURCE, 100_000, 300, toc=None, pages_per_window=30)
    check_common(chunks, 100_000)
    assert len(chunks) == 4
    assert [(c.location_start, c.location_end) for c in chunks] == [
        ("p. 1", "p. 30"),
        ("p. 31", "p. 60"),
        ("p. 61", "p. 90"),
        ("p. 91", "p. 100"),
    ]
    assert "Pages 1-30" in chunks[0].title
    assert "p. 1 - p. 30" in chunks[0].header
    assert "The Book" in chunks[0].header and "R-F-abc" in chunks[0].header


def test_no_toc_large_window_is_split_by_size():
    text = paged_text(40)
    chunk_tokens = 2000
    chunks = chunk_markdown(text, RESOURCE, chunk_tokens, 100, toc=None, pages_per_window=30)
    check_common(chunks, chunk_tokens)
    assert len(chunks) > 2
    seen = []
    for c in chunks:
        seen += [int(p) for p in re.findall(r"<!-- page (\d+) -->", own_text(c.text))]
    assert seen == list(range(1, 41))


def test_transcript_markers():
    segs = []
    for m in range(0, 40):
        segs.append(f"[{m:02d}:00] " + " ".join(f"word{m}_{i}" for i in range(60)) + "\n")
    text = "".join(segs)
    chunk_tokens = 600
    chunks = chunk_markdown(text, {**RESOURCE, "source_type": "youtube"}, chunk_tokens, 300)
    check_common(chunks, chunk_tokens)
    assert len(chunks) > 3
    all_markers = []
    for c in chunks:
        markers = re.findall(r"\[(\d{1,2}:\d{2})\]", c.text)
        assert c.text.startswith("[")
        assert c.location_start == markers[0]
        assert c.location_end == markers[-1]
        all_markers += markers
    assert all_markers == [f"{m:02d}:00" for m in range(40)]
    assert "mm:ss" in chunks[0].header


def test_transcript_hms_markers_and_preamble():
    text = "Intro line without marker.\n[1:02:03] first\n[1:03:03] second\n"
    chunks = chunk_markdown(text, RESOURCE, 1000, 0)
    assert len(chunks) == 1
    assert chunks[0].location_start == "1:02:03" and chunks[0].location_end == "1:03:03"
    assert chunks[0].text.startswith("Intro line")


def test_plain_text_split_at_blank_lines_with_overlap():
    paras = [f"Paragraph {i}. " + " ".join(f"t{i}_{j}" for j in range(40)) for i in range(30)]
    text = "\n\n".join(paras)
    chunk_tokens = 400
    chunks = chunk_markdown(text, {**RESOURCE, "source_type": "txt"}, chunk_tokens, 60)
    check_common(chunks, chunk_tokens)
    assert len(chunks) > 2
    for c in chunks:
        assert c.location_start == "" and c.location_end == ""
        # chunks start and end at paragraph boundaries
        assert own_text(c.text).strip().startswith("Paragraph")
    # the second chunk repeats the tail of the first one as overlap
    assert OVERLAP_START in chunks[1].text
    overlap = chunks[1].text.split(OVERLAP_START, 1)[1].split(OVERLAP_END, 1)[0].strip()
    assert overlap and chunks[0].text.endswith(overlap)
    # own text covers every paragraph exactly once
    seen = []
    for c in chunks:
        seen += re.findall(r"Paragraph (\d+)\.", own_text(c.text))
    assert seen == [str(i) for i in range(30)]
    assert "leave the citation location empty" in chunks[0].header


def test_oversized_single_block_is_hard_split():
    text = "word " * 5000  # one paragraph, no blank lines
    chunk_tokens = 500
    chunks = chunk_markdown(text, RESOURCE, chunk_tokens, 50)
    check_common(chunks, chunk_tokens)
    assert len(chunks) > 5


def test_empty_text_gives_no_chunks():
    assert chunk_markdown("", RESOURCE, 100, 10) == []
    assert chunk_markdown("   \n\n  ", RESOURCE, 100, 10) == []


def test_small_text_is_one_chunk():
    chunks = chunk_markdown("Just a short note.", RESOURCE, 12000, 300)
    assert len(chunks) == 1
    assert chunks[0].text == "Just a short note."
    assert chunks[0].chunk_id == "R-F-abc#00"
    assert estimate_tokens("abcdefgh") == 2
