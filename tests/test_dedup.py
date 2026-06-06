"""Unit tests for the pure near-duplicate matching logic (cite.dedup).

These exercise the metadata comparison in isolation — no library, no I/O — so the
tier rules and normalization are pinned independently of the CLI wiring.
"""

from cite.dedup import (
    FUZZY_TITLE_THRESHOLD,
    find_near_duplicates,
    first_author_family,
    normalize_doi,
    normalize_title,
    title_similarity,
)


def _record(title=None, author=None, year=None, doi=None, new_filename=None):
    rec: dict = {}
    if title is not None:
        rec["title"] = title
    if author is not None:
        rec["author"] = [{"family": fam} for fam in author]
    if year is not None:
        rec["issued"] = {"date-parts": [[year]]}
    if doi is not None:
        rec["DOI"] = doi
    if new_filename is not None:
        rec["_provenance"] = {"new_filename": new_filename}
    return rec


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def test_normalize_doi_strips_url_prefix_and_lowercases():
    assert normalize_doi("https://doi.org/10.1/AbC") == "10.1/abc"
    assert normalize_doi("https://dx.doi.org/10.1/AbC") == "10.1/abc"
    assert normalize_doi("10.1/AbC") == "10.1/abc"
    assert normalize_doi(None) is None
    assert normalize_doi("") is None


def test_normalize_title_strips_punctuation_and_case():
    assert normalize_title("Effects of Drought: A Study!") == "effects of drought a study"
    assert normalize_title(None) == ""


def test_title_similarity_is_order_independent():
    # Stopwords dropped + token set sorted -> word order doesn't matter.
    assert title_similarity("Drought Effects on Wheat", "Effects of Drought on Wheat") == 1.0
    assert title_similarity("totally different", "nothing alike here") < 0.5


def test_first_author_family_falls_back_to_editor():
    assert first_author_family(_record(author=["Smith"])) == "smith"
    assert first_author_family({"editor": [{"family": "Lee"}]}) == "lee"
    assert first_author_family({}) is None


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #


def test_definitive_on_doi_match_ignores_title():
    incoming = _record(title="Some New Title", doi="10.1/x")
    existing = _record(title="A Totally Different Title", doi="https://doi.org/10.1/X")
    [match] = find_near_duplicates(incoming, [existing])
    assert match.tier == "definitive"
    assert match.score == 1.0
    assert match.matched_on == ["doi"]


def test_strong_on_title_author_year_without_doi():
    incoming = _record(title="Phase II Trial of Widget X", author=["Smith"], year=2023)
    existing = _record(
        title="Phase II Trial of Widget X",
        author=["Smith"],
        year=2023,
        new_filename="2023-smith-phase-ii_abc123.pdf",
    )
    [match] = find_near_duplicates(incoming, [existing])
    assert match.tier == "strong"
    assert match.matched_on == ["title", "author", "year"]
    assert match.id == "cite:2023-smith-phase-ii_abc123"


def test_possible_when_title_exact_but_year_differs():
    # Same title + author but a different year is the ambiguous bucket, not strong.
    incoming = _record(title="Annual Survey", author=["Brown"], year=2024)
    existing = _record(title="Annual Survey", author=["Brown"], year=2023)
    [match] = find_near_duplicates(incoming, [existing])
    assert match.tier == "possible"
    assert "title" in match.matched_on and "author" in match.matched_on
    assert "year" not in match.matched_on


def test_possible_on_fuzzy_title():
    incoming = _record(title="Effects of Drought Stress on Wheat Yield")
    existing = _record(title="Effects of Drought Stress on Wheat Yields")
    [match] = find_near_duplicates(incoming, [existing])
    assert match.tier == "possible"
    assert match.score >= FUZZY_TITLE_THRESHOLD


def test_no_match_when_signals_distinct():
    incoming = _record(title="Quantum Computing Basics", author=["Feynman"], year=2001)
    existing = _record(title="Medieval Crop Rotation", author=["Smith"], year=1995)
    assert find_near_duplicates(incoming, [existing]) == []


def test_no_match_when_metadata_absent():
    # No DOI and no usable title on the incoming side -> nothing to compare.
    assert find_near_duplicates({}, [_record(title="Anything")]) == []


def test_results_ordered_definitive_first():
    incoming = _record(title="Shared Title", author=["Smith"], year=2020, doi="10.1/a")
    fuzzy = _record(title="Shared Titles")  # possible
    exact_doi = _record(title="x", doi="10.1/a", new_filename="d.pdf")  # definitive
    matches = find_near_duplicates(incoming, [fuzzy, exact_doi])
    assert [m.tier for m in matches] == ["definitive", "possible"]
