"""Tests for the text-layer probe that chooses the extraction engine.

The probe is what keeps `cite extract` from spending tens of minutes running a
vision model over a document whose text is already perfect, so it is tested
directly against real PDF bytes rather than through docling — no ML, no extra.
"""

from pathlib import Path

from cite.extract.probe import SCANNED, TEXT, probe_text_layer


def _text_pdf(path: Path, n_pages: int = 4, lines_per_page: int = 30) -> Path:
    """Write a minimal born-digital PDF: real page objects with a real text layer.

    Hand-built rather than generated with a library so this test needs no
    dependency beyond pypdf (already core). The structure is the minimum a PDF
    reader accepts: catalog, page tree, one Helvetica font, and per-page content
    streams of `Tj` show-text operators.
    """
    objs: dict[int, bytes] = {}
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(f"{i} 0 R".encode() for i in page_ids)
    objs[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n_pages
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for page_no, pid in enumerate(page_ids, start=1):
        lines = [b"BT /F1 11 Tf 40 750 Td 14 TL"]
        for ln in range(lines_per_page):
            body = f"Page {page_no} line {ln + 1}: the quick brown fox jumps over it."
            lines.append(b"(" + body.encode() + b") Tj T*")
        lines.append(b"ET")
        content = b"\n".join(lines)
        objs[pid] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (pid + 1)
        )
        objs[pid + 1] = (
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    n_objs = max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % n_objs
    for num in range(1, n_objs):
        out += b"%010d 00000 n \n" % offsets.get(num, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        n_objs,
        xref_at,
    )
    path.write_bytes(bytes(out))
    return path


def test_born_digital_pdf_is_read_as_text(tmp_path):
    probe = probe_text_layer(_text_pdf(tmp_path / "book.pdf"))
    assert probe["verdict"] == TEXT
    assert probe["pages"] == 4
    assert probe["median_chars_per_page"] > 200
    assert probe["pages_without_text"] == 0


def test_pdf_without_a_text_layer_is_scanned(tmp_path):
    """Valid pages, no show-text operators — what a scan looks like to pypdf."""
    empty = _text_pdf(tmp_path / "scan.pdf", n_pages=3, lines_per_page=0)
    probe = probe_text_layer(empty)
    assert probe["verdict"] == SCANNED
    assert probe["pages"] == 3
    assert probe["pages_without_text"] == 3


def test_unreadable_input_falls_back_to_the_vlm(tmp_path):
    """Anything we cannot parse must say `scanned` — the VLM can still read it.

    Guessing `text` here would hand the fast engine a file it cannot extract and
    produce empty markdown, which is worse than being slow.
    """
    not_a_pdf = tmp_path / "notes.txt"
    not_a_pdf.write_text("plain text, not a PDF at all")
    assert probe_text_layer(not_a_pdf)["verdict"] == SCANNED

    assert probe_text_layer(tmp_path / "does-not-exist.pdf")["verdict"] == SCANNED


def test_long_documents_are_sampled_not_fully_read(tmp_path):
    """A 60-page document is judged from a bounded sample, evenly spread."""
    big = _text_pdf(tmp_path / "long.pdf", n_pages=60, lines_per_page=25)
    probe = probe_text_layer(big)
    assert probe["pages"] == 60
    assert probe["sampled"] == 20  # capped
    assert probe["verdict"] == TEXT
