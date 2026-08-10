"""Tests for `cite add-supplement` and the supplement read path.

Supplementary material is an *attachment* of its parent reference, so the things
worth proving are mostly about that relationship: it lands in the parent's
bundle, it survives the operations that move the bundle around (update/rename,
central sync, pull, remove), and it never appears as a reference of its own.

Docling is heavy and may be absent, so the document backend is monkeypatched (as
in test_extract.py). The tabular backend is real — it needs nothing but the
standard library for CSV.
"""

import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cite.extract as extract_pkg
from cite.cli import app
from cite.extract import ExtractorUnavailable
from cite.extract.tabular import tabular_to_markdown

from test_extract import _fake_extractor

runner = CliRunner()


def _run(args, input=None, expect_code: int = 0):
    result = runner.invoke(app, args, input=input)
    assert result.exit_code == expect_code, result.output
    return json.loads(result.output)


def _raw(args, expect_code: int = 0) -> str:
    """Invoke the CLI and return stdout verbatim (for `text`, which prints markdown)."""
    result = runner.invoke(app, args)
    assert result.exit_code == expect_code, result.output
    return result.output


def _paper(tmp_path: Path) -> tuple[str, str, str]:
    """Add a reference. Returns (namespaced_id, stem, library_path)."""
    f = tmp_path / "paper.pdf"
    f.write_text("the paper")
    lib = str(tmp_path / "lib")
    _run(["init", "--library", lib, "--yes"])
    added = _run([
        "add", str(f), "--manual", "--type", "journal-article",
        "--field", "title=A Study of Things",
        "--field", "author=Smith, Jane",
        "--field", "container-title=Journal of Things",
        "--field", "issued=2024",
        "--library", lib,
    ])
    return added["id"], added["id"].split(":", 1)[1], lib


@pytest.fixture
def fake_docling(monkeypatch):
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=1))


# --------------------------------------------------------------------------- #
# Attaching
# --------------------------------------------------------------------------- #


def test_document_supplement_is_stored_and_extracted(tmp_path, fake_docling):
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "supporting-information.pdf"
    si.write_text("supporting information")

    out = _run(["add-supplement", rid, str(si), "--label", "SI S1", "--library", lib])

    assert out["status"] == "ok"
    assert (out["n"], out["kind"]) == (1, "document")
    bundle = Path(lib) / stem
    assert (bundle / f"{stem}_supp01.pdf").exists()
    assert (bundle / f"{stem}_supp01.md").exists()
    assert (bundle / f"{stem}_supp01_artifacts").is_dir()

    supp = _run(["get", rid, "--library", lib])["_provenance"]["supplements"]
    assert len(supp) == 1
    assert supp[0]["label"] == "SI S1"
    assert supp[0]["original_filename"] == "supporting-information.pdf"
    assert supp[0]["extraction"]["markdown_path"] == f"{stem}/{stem}_supp01.md"


def test_supplements_are_numbered_in_attach_order(tmp_path, fake_docling):
    rid, stem, lib = _paper(tmp_path)
    for i in (1, 2):
        f = tmp_path / f"si{i}.pdf"
        f.write_text(f"supplement {i}")
        out = _run(["add-supplement", rid, str(f), "--library", lib])
        assert out["n"] == i

    assert (Path(lib) / stem / f"{stem}_supp02.pdf").exists()


def test_reattaching_the_same_bytes_is_a_noop(tmp_path, fake_docling):
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    # Same bytes under a different name — identity is the content, not the name.
    again = tmp_path / "si-copy.pdf"
    again.write_text("supporting information")
    out = _run(["add-supplement", rid, str(again), "--library", lib])

    assert out["status"] == "duplicate"
    assert out["n"] == 1
    assert len(_run(["get", rid, "--library", lib])["_provenance"]["supplements"]) == 1


def test_a_supplement_is_never_a_reference(tmp_path, fake_docling):
    """It must not show up in `list`, only as a count on its parent."""
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    listed = _run(["list", "--library", lib])
    assert listed["count"] == 1
    assert listed["references"][0]["id"] == rid
    assert listed["references"][0]["n_supplements"] == 1


def test_a_reference_without_supplements_stays_unchanged(tmp_path):
    """No empty key in the record, no extra field in the compact listing."""
    rid, stem, lib = _paper(tmp_path)

    assert "supplements" not in _run(["get", rid, "--library", lib])["_provenance"]
    assert "n_supplements" not in _run(["list", "--library", lib])["references"][0]


def test_supplement_on_unknown_id_is_not_found(tmp_path, fake_docling):
    _, _, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("x")

    out = _run(["add-supplement", "cite:nope", str(si), "--library", lib], expect_code=1)
    assert out["status"] == "not_found"


# --------------------------------------------------------------------------- #
# Kind routing: attaching must never fail on an unreadable file
# --------------------------------------------------------------------------- #


def test_binary_supplement_is_stored_without_markdown(tmp_path):
    rid, stem, lib = _paper(tmp_path)
    archive = tmp_path / "raw-data.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data.bin", "0101")

    out = _run(["add-supplement", rid, str(archive), "--library", lib])

    assert out["status"] == "ok"
    assert out["kind"] == "binary"
    assert out["markdown_path"] is None
    assert "warning" not in out  # not extractable is not a failure
    assert (Path(lib) / stem / f"{stem}_supp01.zip").exists()
    assert not (Path(lib) / stem / f"{stem}_supp01.md").exists()


def test_tabular_supplement_becomes_a_markdown_table(tmp_path):
    rid, stem, lib = _paper(tmp_path)
    csv_file = tmp_path / "table-s1.csv"
    csv_file.write_text("plot,yield\nA,3.1\nB,4.2\n")

    out = _run(["add-supplement", rid, str(csv_file), "--label", "Table S1", "--library", lib])

    assert out["kind"] == "tabular"
    md = (Path(lib) / stem / f"{stem}_supp01.md").read_text()
    assert "| plot | yield |" in md
    assert "| A | 3.1 |" in md
    # The heading names the supplement as the reader knows it, not the internal
    # stem — which would go stale the moment the bundle is renamed.
    assert md.startswith("# Table S1")


def test_tabular_heading_falls_back_to_the_original_filename(tmp_path):
    rid, stem, lib = _paper(tmp_path)
    csv_file = tmp_path / "table-s1.csv"
    csv_file.write_text("a,b\n1,2\n")

    _run(["add-supplement", rid, str(csv_file), "--library", lib])

    assert (Path(lib) / stem / f"{stem}_supp01.md").read_text().startswith("# table-s1.csv")


def test_extraction_failure_still_attaches_the_file(tmp_path, monkeypatch):
    """A missing docling extra must cost the text, not the attachment."""
    rid, stem, lib = _paper(tmp_path)

    def _unavailable(*a, **kw):
        raise ExtractorUnavailable("docling is not installed")

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _unavailable)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")

    out = _run(["add-supplement", rid, str(si), "--library", lib])

    assert out["status"] == "ok"
    assert "docling is not installed" in out["warning"]
    assert (Path(lib) / stem / f"{stem}_supp01.pdf").exists()
    supp = _run(["get", rid, "--library", lib])["_provenance"]["supplements"][0]
    assert "extraction" not in supp  # exclude_none: no false claim of extracted text


# --------------------------------------------------------------------------- #
# Reading back
# --------------------------------------------------------------------------- #


def test_text_returns_the_supplement_markdown(tmp_path, fake_docling):
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    body = _raw(["text", rid, "--supplement", "1", "--library", lib])
    assert f"{stem}_supp01.pdf" in body

    path = _raw(["text", rid, "--supplement", "1", "--path-only", "--library", lib])
    assert path.strip().endswith(f"{stem}_supp01.md")


def test_text_for_an_absent_supplement_is_not_found(tmp_path, fake_docling):
    rid, _, lib = _paper(tmp_path)
    out = _run(["text", rid, "--supplement", "9", "--library", lib])
    assert out["status"] == "not_found"
    assert out["n"] == 9


def test_re_extracting_one_supplement(tmp_path, monkeypatch):
    rid, stem, lib = _paper(tmp_path)
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=1))
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    # Re-run with a backend that yields more images; prior output must be replaced.
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=3))
    out = _run(["extract", rid, "--supplement", "1", "--library", lib])

    assert out["status"] == "ok"
    assert out["n_images"] == 3
    artifacts = Path(lib) / stem / f"{stem}_supp01_artifacts"
    assert len(list(artifacts.iterdir())) == 3
    supp = _run(["get", rid, "--library", lib])["_provenance"]["supplements"][0]
    assert supp["extraction"]["n_images"] == 3
    # The reference's own extraction is untouched by a supplement re-extract.
    assert "extraction" not in _run(["get", rid, "--library", lib])["_provenance"]


def test_re_extracting_a_binary_supplement_errors(tmp_path):
    rid, _, lib = _paper(tmp_path)
    archive = tmp_path / "data.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("d.bin", "0")
    _run(["add-supplement", rid, str(archive), "--library", lib])

    out = _run(["extract", rid, "--supplement", "1", "--library", lib], expect_code=1)
    assert out["status"] == "error"
    assert "no extractable text" in out["message"]


# --------------------------------------------------------------------------- #
# Travelling with the parent bundle
# --------------------------------------------------------------------------- #


def test_supplements_survive_a_metadata_edit_that_renames_the_bundle(tmp_path, fake_docling):
    """`cite update` recomputes the id; the supplement must follow the parent."""
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    updated = _run(["update", rid, "--field", "issued=2025", "--library", lib])
    new_id = updated["id"]
    assert new_id != rid
    new_stem = new_id.split(":", 1)[1]

    assert (Path(lib) / new_stem / f"{new_stem}_supp01.pdf").exists()
    assert not (Path(lib) / stem).exists()
    body = _raw(["text", new_id, "--supplement", "1", "--library", lib])
    assert body.strip()

    # The record's pointers into the bundle must be re-stemmed with the files;
    # a stale filename here is a dangling reference doctor would (rightly) flag.
    supp = _run(["get", new_id, "--library", lib])["_provenance"]["supplements"][0]
    assert supp["filename"] == f"{new_stem}_supp01.pdf"
    assert supp["extraction"]["markdown_path"] == f"{new_stem}/{new_stem}_supp01.md"
    assert _run(["doctor", "--library", lib])["status"] == "ok"


def test_removing_a_reference_removes_its_supplements(tmp_path, fake_docling):
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    _run(["remove", rid, "--library", lib])
    assert not (Path(lib) / stem).exists()


def test_a_supplement_reaches_central_and_survives_pull(tmp_path, fake_docling):
    rid, stem, lib = _paper(tmp_path)
    si = tmp_path / "si.pdf"
    si.write_text("supporting information")
    _run(["add-supplement", rid, str(si), "--library", lib])

    other = str(tmp_path / "other-lib")
    _run(["init", "--library", other, "--yes"])
    pulled = _run(["pull", rid, "--library", other])
    assert pulled["status"] == "pulled"

    assert (Path(other) / stem / f"{stem}_supp01.pdf").exists()
    supp = _run(["get", rid, "--library", other])["_provenance"]["supplements"]
    assert len(supp) == 1


# --------------------------------------------------------------------------- #
# Tabular backend (unit)
# --------------------------------------------------------------------------- #


def test_csv_to_markdown_pads_ragged_rows(tmp_path):
    src = tmp_path / "t.csv"
    src.write_text("a,b,c\n1,2\n3,4,5\n")
    md_path = tmp_path / "t.md"

    result = tabular_to_markdown(src, md_path)

    assert result["extractor"] == "cite-tabular"
    assert result["engine"] is None
    lines = md_path.read_text().splitlines()
    assert "| a | b | c |" in lines
    assert "| 1 | 2 |  |" in lines
    assert "| 3 | 4 | 5 |" in lines


def test_csv_cells_containing_pipes_are_escaped(tmp_path):
    src = tmp_path / "t.csv"
    src.write_text('name,note\nA,"x | y"\n')
    md_path = tmp_path / "t.md"

    tabular_to_markdown(src, md_path)

    # An unescaped pipe would split the cell and corrupt the table.
    assert "| A | x \\| y |" in md_path.read_text()


def test_tabular_backend_refuses_a_non_table(tmp_path):
    src = tmp_path / "t.pdf"
    src.write_text("x")
    with pytest.raises(ExtractorUnavailable):
        tabular_to_markdown(src, tmp_path / "t.md")


def test_xlsx_gets_one_section_per_sheet(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    src = tmp_path / "book.xlsx"
    book = openpyxl.Workbook()
    first = book.active
    first.title = "Yields"
    first.append(["plot", "yield"])
    first.append(["A", 3.1])
    second = book.create_sheet("Weather")
    second.append(["day", "rain"])
    second.append([1, 0.4])
    book.save(src)

    md_path = tmp_path / "book.md"
    tabular_to_markdown(src, md_path)

    md = md_path.read_text()
    assert "## Yields" in md and "## Weather" in md
    assert "| plot | yield |" in md
    assert "| day | rain |" in md
