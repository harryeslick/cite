"""Tests for cite.naming — pure, no real I/O for filename logic."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cite.naming import (
    author_components,
    build_filename,
    content_hash,
    record_id,
    short_hash,
)


# ---------------------------------------------------------------------------
# author_components
# ---------------------------------------------------------------------------


def test_author_components_zero():
    assert author_components({}) == ("", "")
    assert author_components({"author": []}) == ("", "")


def test_author_components_one():
    record = {"author": [{"family": "Smith", "given": "J."}]}
    a1, a2 = author_components(record)
    assert a1 == "smith"
    assert a2 == ""


def test_author_components_two():
    record = {"author": [{"family": "Smith"}, {"family": "Lee"}]}
    a1, a2 = author_components(record)
    assert a1 == "smith"
    assert a2 == "lee"


def test_author_components_three_or_more():
    record = {
        "author": [
            {"family": "Smith"},
            {"family": "Lee"},
            {"family": "Jones"},
        ]
    }
    a1, a2 = author_components(record)
    assert a1 == "smith"
    assert a2 == "etal"


def test_author_components_org_literal():
    record = {"author": [{"literal": "World Health Organization"}]}
    a1, a2 = author_components(record)
    assert a1 == "world-health-organization"
    assert a2 == ""


def test_author_components_fallback_to_editor():
    record = {"editor": [{"family": "Brown"}, {"family": "White"}]}
    a1, a2 = author_components(record)
    assert a1 == "brown"
    assert a2 == "white"


def test_author_components_unicode_ascii_fold():
    # Müller -> muller
    record = {"author": [{"family": "Müller"}]}
    a1, _ = author_components(record)
    assert a1 == "muller"


# ---------------------------------------------------------------------------
# build_filename and record_id
# ---------------------------------------------------------------------------

_FAKE_HASH = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"


def _make_record(title, authors, year=None):
    record = {"title": title}
    if authors:
        record["author"] = authors
    if year is not None:
        record["issued"] = {"date-parts": [[year]]}
    return record


def test_build_filename_golden_two_authors():
    record = _make_record(
        "Climate impacts on coastal systems",
        [{"family": "Smith"}, {"family": "Lee"}],
        2021,
    )
    name = build_filename(record, _FAKE_HASH, "pdf")
    assert name.startswith("2021-smith-lee-climate")
    assert name.endswith(f"_{_FAKE_HASH[:6]}.pdf")
    # title slug must be <= 40 chars (the part between author2 and _shorthash)
    stem = name.rsplit(".", 1)[0]  # drop .pdf
    parts = stem.split("-", 3)  # year, a1, a2, rest
    title_and_hash = parts[3]
    title_part = title_and_hash.rsplit("_", 1)[0]
    assert len(title_part) <= 40


def test_build_filename_no_date():
    record = _make_record("A study of things", [{"family": "Jones"}])
    name = build_filename(record, _FAKE_HASH, "pdf")
    # single author -> no second-author slot, no literal placeholder
    assert name.startswith("nd-jones-a-study-of-things")
    assert "-none-" not in name


def test_build_filename_no_ext():
    record = _make_record("Test title", [{"family": "Doe"}], 2020)
    name = build_filename(record, _FAKE_HASH, "")
    assert "." not in name
    assert name.startswith("2020-doe-test-title")
    assert "-none-" not in name


def test_build_filename_single_author_no_placeholder():
    """One author -> `<year>-<author1>-<title>`, no empty second slot."""
    record = _make_record("Solo work", [{"family": "Solo"}], 2019)
    name = build_filename(record, _FAKE_HASH, "pdf")
    assert name == f"2019-solo-solo-work_{_FAKE_HASH[:6]}.pdf"


def test_build_filename_no_authors_omits_author_segment():
    """Zero authors -> `<year>-<title>`, no author segment at all."""
    record = _make_record("Anonymous report", [], 2017)
    name = build_filename(record, _FAKE_HASH, "pdf")
    assert name == f"2017-anonymous-report_{_FAKE_HASH[:6]}.pdf"


def test_build_filename_long_title_truncated():
    long_title = "A very long title that goes well beyond forty characters in total length here"
    record = _make_record(long_title, [{"family": "Author"}], 2022)
    name = build_filename(record, _FAKE_HASH, "pdf")
    stem = name.rsplit(".", 1)[0]
    # single author: year, author1, then the title (split off the first 2 dashes)
    title_part = stem.split("-", 2)[2].rsplit("_", 1)[0]
    assert len(title_part) <= 40


def test_record_id_is_stem_of_build_filename():
    record = _make_record("Something", [{"family": "Test"}], 2023)
    filename = build_filename(record, _FAKE_HASH, "pdf")
    rid = record_id(record, _FAKE_HASH)
    assert filename == f"{rid}.pdf"


def test_record_id_no_ext():
    """record_id should not end with a dot when ext is empty."""
    record = _make_record("Something", [{"family": "Test"}], 2023)
    rid = record_id(record, _FAKE_HASH)
    assert not rid.endswith(".")


# ---------------------------------------------------------------------------
# content_hash and short_hash
# ---------------------------------------------------------------------------


def test_content_hash_determinism(tmp_path: Path):
    f = tmp_path / "sample.bin"
    f.write_bytes(b"hello world" * 1000)
    h1 = content_hash(f)
    h2 = content_hash(f)
    assert h1 == h2
    assert len(h1) == 64
    # Verify it's valid hex
    int(h1, 16)


def test_content_hash_different_content(tmp_path: Path):
    f1 = tmp_path / "a.bin"
    f2 = tmp_path / "b.bin"
    f1.write_bytes(b"aaa")
    f2.write_bytes(b"bbb")
    assert content_hash(f1) != content_hash(f2)


def test_content_hash_matches_stdlib(tmp_path: Path):
    data = b"reference data for hashing"
    f = tmp_path / "ref.bin"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert content_hash(f) == expected


def test_short_hash_default_length():
    full = "abcdef1234567890"
    assert short_hash(full) == "abcdef"
    assert len(short_hash(full)) == 6


def test_short_hash_custom_length():
    full = "abcdef1234567890"
    assert short_hash(full, 8) == "abcdef12"


def test_short_hash_used_in_filename():
    record = _make_record("Title", [{"family": "Doe"}], 2024)
    name = build_filename(record, _FAKE_HASH, "pdf")
    assert _FAKE_HASH[:6] in name
