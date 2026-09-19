"""Tests for pipeline.extract.youtube (transcript shapes -> segments -> markdown)."""

from __future__ import annotations

from pipeline.extract.youtube import normalize_segments, transcript_extraction, transcript_to_markdown

EXPECTED = [
    {"start": 1.5, "duration": 2.0, "text": "Hello & welcome"},
    {"start": 3.5, "duration": 1.0, "text": "to the show"},
]


def test_shape_start_dur_text():
    raw = [
        {"start": "1.5", "dur": "2.0", "text": "Hello &amp; welcome"},
        {"start": 3.5, "dur": 1, "text": " to the\n show "},
    ]
    assert normalize_segments(raw) == EXPECTED


def test_shape_start_duration_text():
    raw = [{"start": 1.5, "duration": 2.0, "text": "Hello & welcome"}, {"start": 3.5, "duration": 1.0, "text": "to the show"}]
    assert normalize_segments(raw) == EXPECTED


def test_shape_offset_duration_text_in_milliseconds():
    raw = [{"offset": 1500, "duration": 2000, "text": "Hello & welcome"}, {"offset": 3500, "duration": 1000, "text": "to the show"}]
    assert normalize_segments(raw) == EXPECTED


def test_shape_offset_duration_text_in_seconds():
    raw = [{"offset": 1.5, "duration": 2.0, "text": "Hello & welcome"}, {"offset": 3.5, "duration": 1.0, "text": "to the show"}]
    assert normalize_segments(raw) == EXPECTED


def test_shape_startms_endms_text():
    raw = [{"startMs": 1500, "endMs": 3500, "text": "Hello & welcome"}, {"startMs": "3500", "endMs": "4500", "text": "to the show"}]
    assert normalize_segments(raw) == EXPECTED


def test_plain_string():
    assert normalize_segments("  just   a string ") == [{"start": 0.0, "duration": 0.0, "text": "just a string"}]
    assert normalize_segments("") == []
    assert normalize_segments([]) == []
    assert normalize_segments(None) == []  # type: ignore[arg-type]


def test_wrapped_dict_and_nested_results():
    assert normalize_segments({"transcript": [{"start": 0, "duration": 5, "text": "wrapped"}]}) == [
        {"start": 0.0, "duration": 5.0, "text": "wrapped"}
    ]
    nested = [{"videoId": "abc", "transcript": [{"start": 0, "dur": 1, "text": "a"}, {"start": 1, "dur": 1, "text": "b"}]}]
    assert [s["text"] for s in normalize_segments(nested)] == ["a", "b"]


def test_integer_seconds_are_not_mistaken_for_ms():
    # a long video with integer second offsets and short durations stays in seconds
    raw = [{"start": 12000, "duration": 4, "text": "late"}, {"start": 12004, "duration": 3, "text": "later"}]
    assert normalize_segments(raw)[0]["start"] == 12000.0
    # integer starts beyond a plausible seconds count, with no durations, are milliseconds
    raw = [{"start": 12000, "text": "a"}, {"start": 15000, "text": "b"}]
    segs = normalize_segments(raw)
    assert segs[0]["start"] == 12.0 and segs[0]["duration"] == 3.0


def test_missing_values_and_sorting():
    raw = [{"start": 10, "text": "second"}, {"start": 0, "text": "first"}, {"start": 5, "text": ""}]
    segs = normalize_segments(raw)
    assert [s["text"] for s in segs] == ["first", "second"]
    assert segs[0]["duration"] == 10.0 and segs[1]["duration"] == 0.0


def test_transcript_to_markdown_windows_and_markers():
    segs = [{"start": i * 10.0, "duration": 10.0, "text": f"segment {i}."} for i in range(13)]
    md = transcript_to_markdown(segs, marker_every=60)
    paragraphs = md.strip().split("\n\n")
    assert paragraphs[0].startswith("[00:00] segment 0. segment 1.")
    assert paragraphs[0].endswith("segment 5.")
    assert paragraphs[1].startswith("[01:00] segment 6.")
    assert paragraphs[2] == "[02:00] segment 12."
    assert md.endswith("\n")
    assert "  " not in md


def test_marker_uses_actual_segment_start_and_hours():
    segs = [
        {"start": 2.0, "duration": 3.0, "text": "early   words"},
        {"start": 3725.4, "duration": 3.0, "text": "an hour later"},
    ]
    md = transcript_to_markdown(segs, marker_every=60)
    assert md == "[00:02] early words\n\n[1:02:05] an hour later\n"


def test_transcript_extraction():
    segs = [{"start": i * 10.0, "duration": 10.0, "text": f"segment {i}."} for i in range(8)]
    ex = transcript_extraction(segs, marker_every=60, language="en")
    assert ex.source_type == "youtube"
    assert ex.transcript_kind == "auto"
    assert ex.duration_sec == 80
    assert ex.metadata.duration_sec == 80
    assert ex.language == "en" and ex.metadata.language == "en"
    assert ex.text.startswith("[00:00] segment 0.")
    assert "[01:00] segment 6." in ex.text
    assert ex.needs_vision is False and ex.warnings == []

    manual = transcript_extraction(segs, 30, transcript_kind="manual", duration_sec=100)
    assert manual.transcript_kind == "manual" and manual.duration_sec == 100
    assert "[00:30]" in manual.text

    empty = transcript_extraction([], 60)
    assert empty.text == "" and empty.transcript_kind == "none"
    assert "empty transcript" in empty.warnings
