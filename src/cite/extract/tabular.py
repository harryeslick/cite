"""Spreadsheet and delimited-text extraction: tables in, markdown out.

Supplementary material is disproportionately *data* — the `.csv` of measurements,
the `.xlsx` with one sheet per experiment — and docling's document pipeline is
both the wrong tool for those and far too heavy a dependency to require for them.
So this is a separate, deliberately small backend beside `docling.py`: CSV and TSV
need nothing but the standard library, and only `.xlsx` reaches for openpyxl.

It returns the same dict shape as `extract_to_markdown`, so `ops` can build an
`Extraction` from either backend without branching on which one ran.
"""

from __future__ import annotations

import csv
import importlib.metadata
from pathlib import Path

from cite.extract.docling import ExtractorUnavailable
from cite.models import TABULAR_SUFFIXES

# A single runaway sheet must not turn a 200 KB supplement into a megabyte of
# agent context. Truncation is recorded in the markdown itself so the reader can
# tell a complete table from a clipped one — a silent cut would be worse than
# either.
MAX_ROWS = 5000

__all__ = ["TABULAR_SUFFIXES", "MAX_ROWS", "tabular_to_markdown"]


def _cell(value: object) -> str:
    """Render one cell as inline markdown: pipes escaped, newlines flattened."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").strip()


def _table(rows: list[list[object]]) -> list[str]:
    """Render rows as a GFM table, taking the first row as the header.

    Ragged rows are padded to the widest row rather than rejected: real-world
    spreadsheets are ragged, and a table that renders is more useful than an
    error about column counts.
    """
    if not rows:
        return ["*(empty)*"]

    width = max(len(r) for r in rows)
    header = [_cell(c) for c in rows[0]] + [""] * (width - len(rows[0]))
    # A wholly blank header row would produce an unreadable table; number instead.
    if not any(header):
        header = [f"col{i + 1}" for i in range(width)]

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    for row in rows[1:]:
        cells = [_cell(c) for c in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _read_delimited(src: Path) -> list[tuple[str, list[list[object]], bool]]:
    """Read a CSV/TSV as a single unnamed sheet. Returns (name, rows, truncated)."""
    delimiter = "\t" if src.suffix.lower() == ".tsv" else ","
    with src.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        rows: list[list[object]] = []
        truncated = False
        for row in reader:
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            rows.append(list(row))
    return [("", rows, truncated)]


def _read_workbook(src: Path) -> list[tuple[str, list[list[object]], bool]]:
    """Read every sheet of an .xlsx/.xls workbook. Returns (name, rows, truncated)."""
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ExtractorUnavailable(
            "reading .xlsx supplements needs openpyxl (install cite[extract])"
        ) from e

    # read_only + values_only: we want values, not formulas or styling, and a
    # supplementary workbook can be large.
    book = openpyxl.load_workbook(src, read_only=True, data_only=True)
    try:
        sheets = []
        for sheet in book.worksheets:
            rows: list[list[object]] = []
            truncated = False
            for row in sheet.iter_rows(values_only=True):
                if len(rows) >= MAX_ROWS:
                    truncated = True
                    break
                rows.append(list(row))
            # Drop trailing all-empty rows, which openpyxl reports generously.
            while rows and not any(_cell(c) for c in rows[-1]):
                rows.pop()
            sheets.append((sheet.title, rows, truncated))
        return sheets
    finally:
        book.close()


def tabular_to_markdown(src: Path, md_path: Path, *, title: str | None = None) -> dict:
    """Convert a CSV/TSV/XLSX at ``src`` to a GFM markdown table file at ``md_path``.

    ``title`` is the document's H1, defaulting to the source filename. Callers
    storing the file under a generated name should pass the *original* filename:
    it is what the reader recognises, and unlike the internal stem it does not go
    stale when the bundle is renamed.

    Each worksheet becomes a ``## <sheet name>`` section; a delimited file has one
    unnamed sheet and so gets no heading. Returns the same dict shape as
    ``extract_to_markdown`` (with ``engine=None`` and ``n_images=0``) so callers
    can record either backend's output as an ``Extraction`` without branching.

    Raises ``ExtractorUnavailable`` for a suffix this backend does not handle, or
    when a workbook needs openpyxl and it is not installed.
    """
    suffix = src.suffix.lower()
    if suffix not in TABULAR_SUFFIXES:
        raise ExtractorUnavailable(f"not a tabular file: {src.name}")

    sheets = _read_workbook(src) if suffix in (".xlsx", ".xls") else _read_delimited(src)

    parts: list[str] = [f"# {title or src.name}", ""]
    for name, rows, truncated in sheets:
        if name:
            parts += [f"## {name}", ""]
        parts += _table(rows)
        if truncated:
            parts += ["", f"*(truncated at {MAX_ROWS} rows)*"]
        parts.append("")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(parts), encoding="utf-8")

    return {
        "markdown_path": str(md_path),
        "artifacts_dir": "",
        "n_images": 0,
        "extractor": "cite-tabular",
        "extractor_version": importlib.metadata.version("cite"),
        # No engine choice exists here — the conversion is deterministic parsing,
        # not a model. None says exactly that.
        "engine": None,
        "probe": None,
        "vlm_model": None,
        "enrichments": [],
        "image_export_mode": "none",
    }
