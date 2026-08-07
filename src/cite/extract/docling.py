"""Docling backend: convert a document to markdown with a local Docling pipeline.

Heavy ML dependency (`docling`, installed via the optional ``[extract]`` extra),
so every docling import is **lazy** — importing this module never pulls docling
in. Callers go through :func:`cite.extract.extract_to_markdown`, which surfaces a
clean :class:`ExtractorUnavailable` when the extra is not installed, keeping the
rest of `cite` usable without it (SUITE.md §7).

Two engines, one output shape:

``text``
    Docling's ``StandardPdfPipeline`` — layout and table-structure models over
    the text the PDF already contains, OCR'ing only pages that have none. The
    right choice for anything born-digital, and roughly an order of magnitude
    faster than the VLM. It cannot invent words, because it never re-reads text
    that is already present.

``vlm``
    ``VlmPipeline`` with granite-docling, mirroring the proven CLI invocation::

        docling <pdf> --to md --image-export-mode referenced \\
            --pipeline vlm --vlm-model granite_docling

    Necessary for scans and image-only documents, where there is no text layer
    to read.

``auto`` (the default) probes the document and picks. See :mod:`cite.extract.probe`.

On top of the ``text`` engine, ``enrich`` turns on docling's optional per-region
models — today ``formula`` and ``code``, which re-read those regions with a small
vision model instead of leaving them undecoded.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

from cite.extract.probe import SCANNED, TEXT, probe_text_layer

# Engine names accepted by extract_to_markdown.
AUTO = "auto"
ENGINES = (AUTO, TEXT, "vlm")
VLM = "vlm"

# Optional enrichment models, run over regions the layout model has already
# found. Text engine only — docling's VlmPipelineOptions has no enrichment
# fields at all, because the vision model already reads those regions itself.
ENRICHMENTS = ("code", "formula")


class ExtractorUnavailable(RuntimeError):
    """The optional ``docling`` extractor is not installed."""


_INSTALL_HINT = (
    "the 'extract' extra is required: `uv sync --extra extract` (dev) "
    "or `uv tool install 'cite[extract]'`"
)


def normalize_enrich(enrich) -> tuple[str, ...]:
    """Accept the many shapes an enrichment selection arrives in; return one.

    A CLI passes ``"formula,code"``, MCP passes the same string, Python callers
    pass a list, and every layer's default is empty. Normalising here — sorted,
    de-duplicated, validated once — means no other layer has to know that
    ``""``, ``None``, ``"formula"`` and ``["formula"]`` are the same request.

    Raises ``ValueError`` naming the valid set, in the same shape as the unknown
    engine error, so the shells can turn either into one message.
    """
    if not enrich:
        return ()
    names = enrich.split(",") if isinstance(enrich, str) else list(enrich)
    selected = {n.strip().lower() for n in names if n.strip()}
    unknown = sorted(selected - set(ENRICHMENTS))
    if unknown:
        raise ValueError(
            f"unknown enrichment {', '.join(repr(u) for u in unknown)}; "
            f"choose from {list(ENRICHMENTS)}"
        )
    return tuple(sorted(selected))


def _resolve_vlm_options(vlm_model: str):
    """Map a model name to docling VLM options.

    Prefer docling's preset registry (forward-compatible with new models); fall
    back to a known spec for the default model on older docling builds.
    """
    try:  # newer, recommended API
        from docling.datamodel.pipeline_options import VlmConvertOptions

        return VlmConvertOptions.from_preset(vlm_model)
    except Exception:
        pass

    from docling.datamodel import vlm_model_specs

    specs = {"granite_docling": getattr(vlm_model_specs, "GRANITEDOCLING_TRANSFORMERS", None)}
    spec = specs.get(vlm_model)
    if spec is None:
        raise ValueError(
            f"unknown vlm_model {vlm_model!r}; this docling build only knows "
            f"{sorted(k for k, v in specs.items() if v is not None)}"
        )
    return spec


def _vlm_converter(vlm_model: str):
    """A converter that reads the document with a vision model, page by page."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import VlmPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.vlm_pipeline import VlmPipeline

    pipeline_options = VlmPipelineOptions(vlm_options=_resolve_vlm_options(vlm_model))
    # REFERENCED export needs rendered picture images; enable whichever toggles
    # this docling build exposes (defensive — VLM options vary across versions).
    for attr in ("generate_picture_images", "generate_page_images"):
        if hasattr(pipeline_options, attr):
            setattr(pipeline_options, attr, True)

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline, pipeline_options=pipeline_options
            )
        }
    )


def _text_converter(probe: dict, enrich: tuple[str, ...] = ()):
    """A converter that reads the document's own text layer.

    Layout and table-structure models still run — they are what turn a flat text
    layer into markdown with correct reading order, headings, and tables, which
    raw text extraction cannot do.

    OCR is enabled only when the probe actually saw pages without text (a mostly
    digital document with a few scanned inserts, which would otherwise come out
    with holes in it). Leaving it on unconditionally costs a ~25 MB model
    download on first run and an engine init on every run, to read pages that we
    have already established do not exist.

    ``enrich`` turns on docling's per-region enrichment models. Off by default:
    each one is a separate VLM (CodeFormulaV2, ~1 GB on first run) that adds
    real time per document, and most documents have no maths or code in them.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = probe.get("pages_without_text", 0) > 0
    pipeline_options.do_table_structure = True
    # Same reason as the VLM path: REFERENCED export needs rendered images.
    for attr in ("generate_picture_images", "generate_page_images"):
        if hasattr(pipeline_options, attr):
            setattr(pipeline_options, attr, True)

    # Defensive setattr for the same reason as the image toggles: these option
    # names have moved between docling versions, and a silently-ignored typo
    # here would produce output that looks fine and is missing the equations.
    for name in enrich:
        attr = f"do_{name}_enrichment"
        if not hasattr(pipeline_options, attr):
            raise ValueError(
                f"this docling build has no {attr}; upgrade docling to use "
                f"--enrich {name}"
            )
        setattr(pipeline_options, attr, True)

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=StandardPdfPipeline, pipeline_options=pipeline_options
            )
        }
    )


def extract_to_markdown(
    src_file: Path,
    md_path: Path,
    *,
    engine: str = AUTO,
    vlm_model: str = "granite_docling",
    enrich=(),
) -> dict:
    """Convert ``src_file`` to markdown at ``md_path`` with referenced images.

    ``engine`` selects how the document is read: ``"text"`` for the text-layer
    pipeline, ``"vlm"`` for the vision model, or ``"auto"`` (the default) to
    probe the document and choose — text layer if it has one, VLM if it doesn't.
    Auto is the right default because the expensive engine is only ever *needed*
    for documents the cheap one cannot read, and the probe costs milliseconds.

    Images are exported in ``REFERENCED`` mode, so docling writes a sibling
    ``<md-stem>_artifacts/`` directory and links the images relatively from the
    markdown. With ``md_path = <id>.md`` inside a bundle, that lands as
    ``<id>_artifacts/`` next to the record (links resolve in place).

    ``enrich`` (``"formula"``, ``"code"``, or both) runs docling's per-region
    enrichment models on the ``text`` engine. Without it, docling leaves a
    ``<!-- formula-not-decoded -->`` placeholder wherever it found an equation
    it would not guess at — so a maths-heavy paper extracts with its equations
    missing rather than mangled. It is off by default because each model is a
    ~1 GB download and adds time to every page that has a hit.

    Returns a summary dict (including the ``engine`` actually used and the
    ``probe`` that chose it); raises :class:`ExtractorUnavailable` if docling is
    not installed, or ``ValueError`` for an unknown engine or enrichment.
    """
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; choose one of {list(ENGINES)}")
    enrich = normalize_enrich(enrich)

    probe = probe_text_layer(src_file)
    if engine == AUTO:
        engine = TEXT if probe["verdict"] == TEXT else VLM

    # Fail rather than drop the request: writing markdown whose provenance
    # claims enrichment that never ran is worse than an error the caller can
    # act on. Reachable via `auto` too, hence the explicit mention of the probe.
    if enrich and engine == VLM:
        raise ValueError(
            f"enrichment ({', '.join(enrich)}) is only available on the text "
            f"engine; this document resolved to the vlm engine "
            f"(probe verdict: {probe['verdict']}). Re-run with --engine text to "
            f"force the text pipeline, or drop --enrich."
        )

    try:
        from docling_core.types.doc import ImageRefMode

        converter = (
            _text_converter(probe, enrich)
            if engine == TEXT
            else _vlm_converter(vlm_model)
        )
    except ImportError as e:  # extra not installed
        raise ExtractorUnavailable(_INSTALL_HINT) from e

    result = converter.convert(str(src_file))
    md_path.parent.mkdir(parents=True, exist_ok=True)

    # Pass artifacts_dir as a *bare* relative name (no parent components). Docling
    # then sets reference_path = md_path.parent and writes the images beside the
    # markdown with *relative* links (`<stem>_artifacts/...`) — exactly what a
    # self-contained bundle needs. The default (None) instead auto-derives a path
    # that already includes md_path.parent and re-joins it, producing a spurious
    # nested dir for multi-component relative md paths; an absolute artifacts_dir
    # would null out reference_path and bake absolute links into the markdown.
    artifacts_name = f"{md_path.stem}_artifacts"
    result.document.save_as_markdown(
        md_path, artifacts_dir=Path(artifacts_name), image_mode=ImageRefMode.REFERENCED
    )

    artifacts_dir = md_path.parent / artifacts_name
    n_images = sum(1 for _ in artifacts_dir.iterdir()) if artifacts_dir.is_dir() else 0

    return {
        "markdown_path": str(md_path),
        "artifacts_dir": str(artifacts_dir),
        "n_images": n_images,
        "extractor": "docling",
        "extractor_version": importlib.metadata.version("docling"),
        "engine": engine,
        "probe": probe,
        # Only meaningful on the vlm engine; None records "no vision model ran",
        # which is what makes a stored extraction's provenance honest.
        "vlm_model": vlm_model if engine == VLM else None,
        "enrichments": list(enrich),
        "image_export_mode": "referenced",
    }
