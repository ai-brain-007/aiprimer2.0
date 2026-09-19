from pipeline.kb.verify import dedupe_within_resource, normalize_text, quote_in_source, verify_outputs
from pipeline.models import Citation, ExtractedCitation, ExtractedUnit, ExtractionOutput, VerifiedUnit

SOURCE = """<!-- page 3 -->

# Chapter One

The *quick* brown fox jumps over the lazy dog. It said "don't panic" and went on.

> Sleep is the foundation of recovery; without it nothing else
> in this book will work.

[12:30] Some transcript-style text here.
"""


def test_normalize_text_strips_markers_and_quotes():
    n = normalize_text("<!-- page 3 -->\n# Title\n\n*Bold* “quoted” text – with [01:02] marker\n> and quote")
    assert n == 'title bold "quoted" text - with marker and quote'
    assert normalize_text("") == ""
    assert normalize_text("infor-\nmation") == "information"


def test_exact_match_scores_100():
    ok, score = quote_in_source("The quick brown fox jumps over the lazy dog.", SOURCE, 90)
    assert ok and score == 100
    ok, score = quote_in_source('It said "don\'t panic"', SOURCE, 90)
    assert ok and score == 100


def test_fuzzy_match_with_punctuation_and_line_breaks():
    quote = "Sleep is the foundation of recovery: without it, nothing else in this book will work"
    ok, score = quote_in_source(quote, SOURCE, 90)
    assert ok and 90 <= score <= 100
    # curly quotes and markdown emphasis in the quote are fine
    ok, score = quote_in_source("It said “don’t panic” and went on", SOURCE, 90)
    assert ok and score == 100


def test_rejection_below_threshold_and_empty_quote():
    ok, score = quote_in_source("Completely unrelated sentence about quantum chromodynamics", SOURCE, 90)
    assert not ok and score < 90
    assert quote_in_source("", SOURCE, 90) == (False, 0)
    assert quote_in_source("something", "", 90) == (False, 0)


def test_long_source_window_path():
    filler = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor " * 20 + "\n\n") * 180
    assert len(filler) > 200_000
    needle = "This precise sentence only appears once near the very end of the document."
    source = filler + "\n\n" + needle + "\n\n" + filler[:30_000]
    ok, score = quote_in_source(needle, source, 90)
    assert ok and score == 100
    # fuzzy variant must also be found through the window path
    ok, score = quote_in_source("This precise sentence only appears once near the very end of the document", source, 90)
    assert ok and score >= 90
    ok, score = quote_in_source("An entirely different claim about photosynthesis in deep sea vents.", source, 90)
    assert not ok


def make_output(units, chunk_id="R-1#00"):
    return ExtractionOutput(resource_id="R-1", chunk_id=chunk_id, units=units)


def test_verify_outputs_marks_and_rejects():
    good = ExtractedUnit(
        type="principle",
        name="Sleep first",
        description="Sleep underpins recovery.",
        details="Without sleep nothing works.",
        citations=[
            ExtractedCitation(location="p. 3", quote="Sleep is the foundation of recovery"),
            ExtractedCitation(location="p. 4", quote="this quote is invented and appears nowhere"),
        ],
    )
    bad = ExtractedUnit(
        type="claim",
        name="Invented",
        description="Made up.",
        citations=[ExtractedCitation(location="p. 9", quote="nothing like this is in the text at all")],
    )
    short = ExtractedUnit(type="claim", name="X", description="d", citations=[ExtractedCitation(quote="quick brown fox")])
    nodesc = ExtractedUnit(type="claim", name="No description", description="", citations=[ExtractedCitation(quote="quick brown fox")])
    badtype = ExtractedUnit.model_construct(
        type="poem", name="Bad type", description="d", aliases=[], details="", notes="",
        citations=[ExtractedCitation(quote="quick brown fox")], confidence=None, stage_hint="",
    )
    verified, rejects = verify_outputs([make_output([good, bad, short, nodesc, badtype])], {"R-1#00": SOURCE}, SOURCE, 90)
    assert [v.name for v in verified] == ["Sleep first"]
    v = verified[0]
    assert isinstance(v, VerifiedUnit)
    assert v.temp_id == "R-1:R-1#00:0"
    assert len(v.verified_citations) == 1
    c = v.verified_citations[0]
    assert isinstance(c, Citation)
    assert c.resource_id == "R-1" and c.location == "p. 3" and c.verified and c.score >= 90
    assert v.citations == good.citations  # the raw extracted citations are kept too
    reasons = {r["temp_id"]: r for r in rejects}
    assert set(reasons) == {"R-1:R-1#00:1", "R-1:R-1#00:2", "R-1:R-1#00:3", "R-1:R-1#00:4"}
    assert "no citation" in reasons["R-1:R-1#00:1"]["reason"] and reasons["R-1:R-1#00:1"]["best_score"] < 90
    assert "name" in reasons["R-1:R-1#00:2"]["reason"]
    assert "description" in reasons["R-1:R-1#00:3"]["reason"]
    assert "type" in reasons["R-1:R-1#00:4"]["reason"]
    for r in rejects:
        assert set(r) == {"temp_id", "name", "reason", "best_score"}


def test_citation_found_in_full_text_when_not_in_chunk():
    unit = ExtractedUnit(
        type="concept", name="Lazy dog", description="The dog.", citations=[ExtractedCitation(quote="the lazy dog")]
    )
    verified, rejects = verify_outputs([make_output([unit], chunk_id="R-1#01")], {"R-1#01": "unrelated chunk"}, SOURCE, 90)
    assert len(verified) == 1 and not rejects
    assert verified[0].verified_citations[0].score == 100


def vunit(name, temp_id, details="", aliases=None, quotes=("q",)):
    return VerifiedUnit(
        temp_id=temp_id,
        type="technique",
        name=name,
        aliases=aliases or [],
        description=f"{name} description",
        details=details,
        citations=[ExtractedCitation(location="p. 1", quote=q) for q in quotes],
        verified_citations=[Citation(resource_id="R-1", location="p. 1", quote=q, verified=True, score=100) for q in quotes],
    )


def test_dedupe_within_resource():
    a = vunit("Box breathing", "t0", details="short", aliases=["Square breathing"], quotes=("q1",))
    b = vunit("Box Breathing", "t1", details="a much longer details text", aliases=["4x4 breath"], quotes=("q1", "q2"))
    c = vunit("Cold exposure", "t2", details="cold", quotes=("q3",))
    d = vunit("The box breathing technique", "t3", details="", quotes=("q4",))
    out = dedupe_within_resource([a, b, c, d])
    assert [u.name for u in out] == ["Box breathing", "Cold exposure"]
    merged = out[0]
    assert merged.temp_id == "t0"
    assert merged.details == "a much longer details text"
    # "Box Breathing" normalizes to the kept name, so it is not added as an alias
    assert set(merged.aliases) == {"Square breathing", "4x4 breath", "The box breathing technique"}
    assert [x.quote for x in merged.verified_citations] == ["q1", "q2", "q4"]
    assert [x.quote for x in merged.citations] == ["q1", "q2", "q4"]
    # inputs are not mutated
    assert a.details == "short" and a.aliases == ["Square breathing"]
