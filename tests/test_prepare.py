"""Tests for `cite prepare` (pre-add extraction) and its adoption by `cite add`.

The real Docling engine is heavy and may be absent, so these monkeypatch the
extraction backend with a stand-in that writes a fake markdown + artifacts (the
same fixture style as test_extract.py). They exercise the wiring: the staging
cache, the markdown head/identifier scan, graceful degradation when the extra is
missing, and — the crux — that `add` adopts a staged extraction into the bundle
*without re-running* the VLM (SUITE.md §12: run the real CLI; optional features
degrade without the dependency).
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


def _fake_extractor(n_images: int = 1, body: str = "", *, calls: list | None = None):
    """Stand-in for extract_to_markdown that writes a fake md + artifacts.

    If `calls` is given, each invocation appends the src path — letting a test
    assert the VLM ran exactly once (i.e. add adopted rather than re-extracting).
    """

    def _fake(src_file: Path, md_path: Path, *, vlm_model: str = "granite_docling") -> dict:
        if calls is not None:
            calls.append(str(src_file))
        md_path.parent.mkdir(parents=True, exist_ok=True)
        artifacts = md_path.parent / f"{md_path.stem}_artifacts"
        artifacts.mkdir(exist_ok=True)
        links = ""
        for i in range(n_images):
            img = artifacts / f"image_{i}.png"
            img.write_bytes(b"\x89PNG")
            links += f"\n![img]({artifacts.name}/{img.name})\n"
        md_path.write_text(f"# Annual Drought Report 2023\n\nDept of Water\n{body}{links}")
        return {
            "markdown_path": str(md_path),
            "artifacts_dir": str(artifacts),
            "n_images": n_images,
            "extractor": "docling",
            "extractor_version": "9.9.9-fake",
            "vlm_model": vlm_model,
            "image_export_mode": "referenced",
        }

    return _fake


def _report(tmp_path: Path) -> tuple[Path, str]:
    f = tmp_path / "report.pdf"
    f.write_text("a council drought report with no DOI and empty metadata")
    return f, str(tmp_path / "lib")


def test_prepare_stages_markdown_and_returns_head(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(n_images=2))

    out = _run(["prepare", str(f), "--library", lib])
    assert out["status"] == "staged"
    assert out["n_images"] == 2
    # Head + title guess come from the *extracted* text (not pypdf).
    assert out["head"].startswith("# Annual Drought Report 2023")
    assert out["title_guess"] == "Annual Drought Report 2023"
    assert out["doi"] is None  # report carries none

    # Markdown is cached under the content hash, not in any bundle yet.
    staging = Path(lib) / ".staging" / out["file_hash"]
    assert (staging / f"{out['file_hash']}.md").exists()
    assert json.loads((staging / "meta.json").read_text())["extractor"] == "docling"
    # Nothing has been added to the library proper.
    assert _run(["list", "--library", lib])["count"] == 0


def test_prepare_scans_doi_from_extracted_text(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)
    body = "\nAvailable online. https://doi.org/10.1234/abcd.5678\n"
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(body=body))

    out = _run(["prepare", str(f), "--library", lib])
    assert out["doi"] == "10.1234/abcd.5678"
    assert out["suggested_next"] == "cite search --doi 10.1234/abcd.5678"


def test_prepare_head_truncation(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)
    big = "\n".join(f"line {i} of the body" for i in range(500))
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(body=big))

    out = _run(["prepare", str(f), "--head-chars", "120", "--library", lib])
    assert out["head_truncated"] is True
    assert len(out["head"]) <= 120


def test_add_adopts_staged_extraction_without_reextracting(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)
    calls: list = []
    monkeypatch.setattr(
        extract_pkg, "extract_to_markdown", _fake_extractor(n_images=2, calls=calls)
    )

    prep = _run(["prepare", str(f), "--library", lib])
    assert len(calls) == 1  # VLM ran once, in prepare

    added = _run([
        "add", str(f), "--manual", "--type", "other-report",
        "--field", "title=Annual Drought Report 2023",
        "--field", "publisher=Dept of Water",
        "--field", "issued=2023",
        "--library", lib,
    ])
    assert len(calls) == 1  # add did NOT re-extract — it adopted the cache
    assert added["status"] == "added"
    assert added["extracted"] is True

    stem = added["id"].split(":", 1)[1]
    assert added["markdown_path"] == f"{stem}/{stem}.md"

    bundle = Path(lib) / stem
    assert (bundle / f"{stem}.md").exists()
    assert (bundle / f"{stem}_artifacts").is_dir()
    assert len(list((bundle / f"{stem}_artifacts").iterdir())) == 2

    # Image links were rewritten from the hash stem to the id stem.
    md = (bundle / f"{stem}.md").read_text()
    assert f"{stem}_artifacts/" in md
    assert prep["file_hash"] not in md

    # Extraction provenance is recorded, tying the text to the stored bytes.
    record = _run(["get", added["id"], "--library", lib])
    ext = record["_provenance"]["extraction"]
    assert ext["extractor_version"] == "9.9.9-fake"
    assert ext["markdown_path"] == f"{stem}/{stem}.md"
    assert ext["source_file_hash"] == record["_provenance"]["file_hash"]
    assert ext["n_images"] == 2

    # The staging entry is consumed on adoption.
    assert not (Path(lib) / ".staging" / prep["file_hash"]).exists()


def test_add_without_prepare_reports_not_extracted(tmp_path):
    f, lib = _report(tmp_path)
    added = _run([
        "add", str(f), "--manual", "--type", "other-report",
        "--field", "title=Annual Drought Report 2023",
        "--field", "publisher=Dept of Water",
        "--field", "issued=2023",
        "--library", lib,
    ])
    assert added["extracted"] is False
    assert added["markdown_path"] is None


def test_prepare_is_idempotent(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)
    calls: list = []
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor(calls=calls))

    first = _run(["prepare", str(f), "--library", lib])
    second = _run(["prepare", str(f), "--library", lib])
    assert len(calls) == 1  # second call reused the staged extraction
    assert first["file_hash"] == second["file_hash"]
    assert second["status"] == "staged"


def test_prepare_short_circuits_when_already_in_library(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)
    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _fake_extractor())
    _run(["prepare", str(f), "--library", lib])
    _run([
        "add", str(f), "--manual", "--type", "other-report",
        "--field", "title=Annual Drought Report 2023",
        "--field", "publisher=Dept of Water",
        "--field", "issued=2023",
        "--library", lib,
    ])

    out = _run(["prepare", str(f), "--library", lib])
    assert out["status"] == "duplicate"
    assert "text" in out["hint"]


def test_prepare_graceful_when_extractor_unavailable(tmp_path, monkeypatch):
    f, lib = _report(tmp_path)

    def _unavailable(*a, **k):
        raise ExtractorUnavailable("the 'extract' extra is required")

    monkeypatch.setattr(extract_pkg, "extract_to_markdown", _unavailable)

    # Clean redirect (exit 0, not a crash) pointing at the deterministic fallback.
    out = _run(["prepare", str(f), "--library", lib])
    assert out["status"] == "extractor_unavailable"
    assert out["suggested_next"] == f"cite peek {f}"
    # No staging residue left behind.
    assert not (Path(lib) / ".staging").exists() or not any(
        (Path(lib) / ".staging").iterdir()
    )


# --------------------------------------------------------------------------- #
# Real docling pipeline — slow, network on first run (model download), skipped
# when the optional `extract` extra is not installed.
# --------------------------------------------------------------------------- #

docling = pytest.importorskip("docling", reason="needs the optional 'extract' extra")
PIL = pytest.importorskip("PIL", reason="needs the optional 'extract' extra (Pillow)")

import shutil  # noqa: E402
import subprocess  # noqa: E402

CITE_BIN = shutil.which("cite")


def _cli(args: list[str]) -> dict:
    """Run the real `cite` binary as a subprocess and parse its stdout JSON.

    The real engine must be exercised the way agents and the MCP wrapper use it —
    a separate process where docling/transformers log to *stderr*, leaving stdout
    a single clean JSON object. (In-process CliRunner shares one stdout buffer, so
    those libraries' incidental writes would corrupt the envelope.)
    """
    proc = subprocess.run([CITE_BIN, *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.slow
@pytest.mark.skipif(CITE_BIN is None, reason="`cite` not on PATH")
def test_real_prepare_then_add_adopts_extraction(tmp_path):
    """End-to-end with the real VLM: prepare a PDF, then add adopts its markdown."""
    from PIL import Image, ImageDraw

    src = tmp_path / "council-report.pdf"
    page = Image.new("RGB", (1000, 1300), "white")
    draw = ImageDraw.Draw(page)
    draw.text((80, 60), "Annual Drought Report 2023", fill="black")
    draw.rectangle([200, 300, 800, 900], fill=(40, 110, 200), outline="black", width=4)
    page.save(src, "PDF", resolution=100.0)
    lib = str(tmp_path / "lib")

    prep = _cli(["prepare", str(src), "--library", lib])
    assert prep["status"] == "staged"
    assert prep["head"]  # some markdown came back to read

    added = _cli([
        "add", str(src), "--manual", "--type", "other-report",
        "--field", "title=Annual Drought Report 2023",
        "--field", "publisher=Council",
        "--field", "issued=2023",
        "--library", lib,
    ])
    assert added["extracted"] is True
    stem = added["id"].split(":", 1)[1]
    assert (Path(lib) / stem / f"{stem}.md").exists()
    # Staging consumed; markdown links (if any) resolve to the id stem.
    assert not (Path(lib) / ".staging" / prep["file_hash"]).exists()
    md = (Path(lib) / stem / f"{stem}.md").read_text()
    assert prep["file_hash"] not in md
