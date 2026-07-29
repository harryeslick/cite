"""Tests for `cite extract` / `cite text` wiring.

The real Docling engine is heavy and may be absent, so these tests monkeypatch
the extraction backend to simulate it (write a fake markdown + artifact). They
exercise the CLI wiring — bundle storage, provenance, idempotent overwrite, and
graceful degradation — per SUITE.md §12 (run the real CLI; sibling/optional
features degrade without the dependency installed).
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cite.extract as extract_pkg
from cite.cli import app
from cite.extract import ExtractorUnavailable

runner = CliRunner()


def _run(args, input=None, expect_code: int = 0):
    result = runner.invoke(app, args, input=input)
    assert result.exit_code == expect_code, result.output
    return json.loads(result.output)


def _add_ref(tmp_path: Path) -> tuple[str, str]:
    """Add a reference via the CLI; return (namespaced_id, library_path)."""
    f = tmp_path / "report.pdf"
    f.write_text("internal report contents")
    lib = str(tmp_path / "lib")
    _run(["init", "--library", lib, "--yes"])
    added = _run([
        "add", str(f), "--manual", "--type", "other-report",
        "--field", "title=Annual Report",
        "--field", "publisher=Agency",
        "--field", "issued=2022",
        "--library", lib,
    ])
    return added["id"], lib


def _fake_extractor(n_images: int = 1, resolved_engine: str = "text"):
    """Build a stand-in for extract_to_markdown that writes a fake md + artifacts.

    ``resolved_engine`` stands in for what the real backend's auto-probe would
    have chosen, so the wiring tests can assert the engine reaches provenance
    without running docling.
    """

    def _fake(
        src_file: Path,
        md_path: Path,
        *,
        engine: str = "auto",
        vlm_model: str = "granite_docling",
    ) -> dict:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        artifacts = md_path.parent / f"{md_path.stem}_artifacts"
        artifacts.mkdir(exist_ok=True)
        links = ""
        for i in range(n_images):
            img = artifacts / f"image_{i}.png"
            img.write_bytes(b"\x89PNG")
            links += f"\n![img]({artifacts.name}/{img.name})\n"
        md_path.write_text(f"# {src_file.name}\n{links}")
        used = resolved_engine if engine == "auto" else engine
        return {
            "markdown_path": str(md_path),
            "artifacts_dir": str(artifacts),
            "n_images": n_images,
            "extractor": "docling",
            "extractor_version": "9.9.9-fake",
            "engine": used,
            "probe": {"verdict": "text", "median_chars_per_page": 3000},
            "vlm_model": vlm_model if used == "vlm" else None,
            "image_export_mode": "referenced",
        }

    return _fake


def test_extract_stores_markdown_and_provenance(tmp_path, monkeypatch):
    rid, lib = _add_ref(tmp_path)
    stem = rid.split(":", 1)[1]
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=2))

    out = _run(["extract", rid, "--library", lib])
    assert out["status"] == "ok"
    assert out["id"] == rid
    assert out["markdown_path"] == f"{stem}/{stem}.md"
    assert out["n_images"] == 2
    assert out["extractor_version"] == "9.9.9-fake"

    # Markdown + artifacts land inside the reference bundle.
    bundle = Path(lib) / stem
    assert (bundle / f"{stem}.md").exists()
    assert (bundle / f"{stem}_artifacts").is_dir()
    assert len(list((bundle / f"{stem}_artifacts").iterdir())) == 2

    # Provenance records the extraction (self-tracking).
    record = _run(["get", rid, "--library", lib])
    ext = record["_provenance"]["extraction"]
    assert ext["extractor"] == "docling"
    # The text engine ran, so no vision model is claimed in provenance.
    assert ext["engine"] == "text"
    assert "vlm_model" not in ext
    assert ext["image_export_mode"] == "referenced"
    assert ext["markdown_path"] == f"{stem}/{stem}.md"
    assert ext["n_images"] == 2
    # source_file_hash ties the text to the exact stored bytes.
    assert ext["source_file_hash"] == record["_provenance"]["file_hash"]


def test_text_prints_markdown_and_path(tmp_path, monkeypatch):
    rid, lib = _add_ref(tmp_path)
    stem = rid.split(":", 1)[1]
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor())
    _run(["extract", rid, "--library", lib])

    # Content form prints raw markdown (not JSON).
    content = runner.invoke(app, ["text", rid, "--library", lib])
    assert content.exit_code == 0
    assert content.output.startswith("# ")

    # Path-only form prints the markdown path.
    path_out = runner.invoke(app, ["text", rid, "--path-only", "--library", lib])
    assert path_out.exit_code == 0
    assert path_out.output.strip().endswith(f"{stem}.md")


def test_text_not_found_before_extract(tmp_path):
    rid, lib = _add_ref(tmp_path)
    out = _run(["text", rid, "--library", lib])
    assert out["status"] == "not_found"
    assert "extract" in out["hint"]


def test_extract_idempotent_overwrite(tmp_path, monkeypatch):
    rid, lib = _add_ref(tmp_path)
    stem = rid.split(":", 1)[1]
    artifacts = Path(lib) / stem / f"{stem}_artifacts"

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=3))
    _run(["extract", rid, "--library", lib])
    assert len(list(artifacts.iterdir())) == 3

    # Re-extract with fewer images: clear_text must wipe the stale artifacts first.
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=1))
    out = _run(["extract", rid, "--library", lib])
    assert out["n_images"] == 1
    assert len(list(artifacts.iterdir())) == 1  # no leftover stale images


def test_extract_missing_reference(tmp_path):
    """An unknown id in a real library is not_found (vs no_library for a bad root)."""
    lib = str(tmp_path / "lib")
    _run(["init", "--library", lib, "--yes"])
    out = _run(["extract", "cite:does-not-exist", "--library", lib])
    assert out["status"] == "not_found"


def test_engine_flag_reaches_the_backend_and_provenance(tmp_path, monkeypatch):
    """`--engine vlm` forces the vision model, and the record says so.

    Which engine produced the markdown is not cosmetic: a text-layer and a VLM
    extraction of the same document are different artefacts, and only the record
    can tell you later which one you have.
    """
    rid, lib = _add_ref(tmp_path)
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor())

    out = _run(["extract", rid, "--engine", "vlm", "--library", lib])
    assert out["engine"] == "vlm"

    ext = _run(["get", rid, "--library", lib])["_provenance"]["extraction"]
    assert ext["engine"] == "vlm"
    assert ext["vlm_model"] == "granite_docling"


def test_auto_is_the_default_engine(tmp_path, monkeypatch):
    """With no flag, the backend is asked to choose — here it picks the text layer."""
    rid, lib = _add_ref(tmp_path)
    monkeypatch.setattr(
        extract_pkg, "extract_to_markdown", _fake_extractor(resolved_engine="text")
    )

    out = _run(["extract", rid, "--library", lib])
    assert out["engine"] == "text"
    assert out["probe"]["verdict"] == "text"


def test_unknown_engine_is_rejected(tmp_path):
    rid, lib = _add_ref(tmp_path)
    result = runner.invoke(app, ["extract", rid, "--engine", "ocr", "--library", lib])
    assert result.exit_code != 0
    assert "auto, text, vlm" in result.output


def test_extract_graceful_degradation_when_docling_absent(tmp_path, monkeypatch):
    rid, lib = _add_ref(tmp_path)

    def _unavailable(*a, **k):
        raise ExtractorUnavailable("the 'extract' extra is required")

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _unavailable)
    result = runner.invoke(app, ["extract", rid, "--library", lib])
    assert result.exit_code != 0  # error status is non-zero (SUITE.md §3.1)
    out = json.loads(result.output)
    assert out["status"] == "error"
    assert "extract" in out["hint"]


# --------------------------------------------------------------------------- #
# Standalone extraction (no library)
# --------------------------------------------------------------------------- #


def test_extract_file_standalone(tmp_path, monkeypatch):
    src = tmp_path / "report.pdf"
    src.write_text("fake pdf contents")
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=2))

    out = _run(["extract", str(src)])
    assert out["status"] == "ok"
    assert "id" not in out
    assert out["source"] == str(src.resolve())
    assert out["markdown_path"] == str((tmp_path / "report.md").resolve())
    assert out["n_images"] == 2

    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "report_artifacts").is_dir()
    assert len(list((tmp_path / "report_artifacts").iterdir())) == 2


def test_extract_file_standalone_idempotent(tmp_path, monkeypatch):
    src = tmp_path / "paper.pdf"
    src.write_text("fake pdf")

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=3))
    _run(["extract", str(src)])
    assert len(list((tmp_path / "paper_artifacts").iterdir())) == 3

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=1))
    out = _run(["extract", str(src)])
    assert out["n_images"] == 1
    assert len(list((tmp_path / "paper_artifacts").iterdir())) == 1


def test_extract_file_standalone_missing(tmp_path):
    result = runner.invoke(app, ["extract", str(tmp_path / "nonexistent.pdf")])
    assert result.exit_code != 0
    out = json.loads(result.output)
    assert out["status"] == "error"
    assert "not found" in out["message"]


def test_extract_file_standalone_no_extractor(tmp_path, monkeypatch):
    src = tmp_path / "doc.pdf"
    src.write_text("fake pdf")

    def _unavailable(*a, **k):
        raise ExtractorUnavailable("the 'extract' extra is required")

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _unavailable)
    result = runner.invoke(app, ["extract", str(src)])
    assert result.exit_code != 0
    out = json.loads(result.output)
    assert out["status"] == "error"
    assert "extract" in out["hint"]


# --------------------------------------------------------------------------- #
# Real docling pipeline — slow, network on first run (model download), skipped
# when the optional `extract` extra is not installed.
# --------------------------------------------------------------------------- #

docling = pytest.importorskip("docling", reason="needs the optional 'extract' extra")
PIL = pytest.importorskip("PIL", reason="needs the optional 'extract' extra (Pillow)")


@pytest.mark.slow
def test_real_docling_places_artifacts_beside_markdown(tmp_path, monkeypatch):
    """Regression for the docling artifacts double-join bug.

    With a *relative, multi-component* markdown path (the bundle case), a default
    artifacts_dir made docling nest `<parent>/<parent>/<stem>_artifacts`. We pass a
    bare artifacts name so images land in `<stem>_artifacts/` beside the markdown
    with relative links. This test reproduces the original failing condition
    (relative md path under a working dir) and asserts the corrected placement.
    """
    from PIL import Image, ImageDraw

    from cite.extract import extract_to_markdown

    # A one-page PDF with a single clear figure so docling emits one picture.
    src = tmp_path / "figure.pdf"
    page = Image.new("RGB", (1000, 1300), "white")
    draw = ImageDraw.Draw(page)
    draw.text((80, 60), "Figure Test", fill="black")
    draw.rectangle([200, 300, 800, 900], fill=(40, 110, 200), outline="black", width=4)
    page.save(src, "PDF", resolution=100.0)

    stem = "2024-fig_abcdef"
    monkeypatch.chdir(tmp_path)  # exercise a *relative* md path (the bug trigger)
    md_path = Path("lib") / stem / f"{stem}.md"

    result = extract_to_markdown(src, md_path, vlm_model="granite_docling")

    artifacts = md_path.parent / f"{stem}_artifacts"
    assert artifacts.is_dir()  # placed beside the markdown...
    assert not (md_path.parent / "lib").exists()  # ...not double-joined/nested
    assert result["n_images"] >= 1
    # Image links are relative to the markdown (bundle stays portable).
    md = md_path.read_text()
    assert f"{stem}_artifacts/" in md
    assert "![" in md
