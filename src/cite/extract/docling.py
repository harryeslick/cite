"""Docling backend: convert a document to markdown with a local VLM pipeline.

Heavy ML dependency (`docling`, installed via the optional ``[extract]`` extra),
so every docling import is **lazy** — importing this module never pulls docling
in. Callers go through :func:`cite.extract.extract_to_markdown`, which surfaces a
clean :class:`ExtractorUnavailable` when the extra is not installed, keeping the
rest of `cite` usable without it (SUITE.md §7).

The pipeline mirrors the user's proven CLI invocation in API form::

    docling <pdf> --to md --image-export-mode referenced \\
        --pipeline vlm --vlm-model granite_docling
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path


class ExtractorUnavailable(RuntimeError):
    """The optional ``docling`` extractor is not installed."""


_INSTALL_HINT = (
    "the 'extract' extra is required: `uv sync --extra extract` (dev) "
    "or `uv tool install 'cite[extract]'`"
)


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


def extract_to_markdown(
    src_file: Path, md_path: Path, *, vlm_model: str = "granite_docling"
) -> dict:
    """Convert ``src_file`` to markdown at ``md_path`` with referenced images.

    Images are exported in ``REFERENCED`` mode, so docling writes a sibling
    ``<md-stem>_artifacts/`` directory and links the images relatively from the
    markdown. With ``md_path = <id>.md`` inside a bundle, that lands as
    ``<id>_artifacts/`` next to the record (links resolve in place).

    Returns a summary dict; raises :class:`ExtractorUnavailable` if docling is
    not installed.
    """
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import VlmPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.pipeline.vlm_pipeline import VlmPipeline
        from docling_core.types.doc import ImageRefMode
    except ImportError as e:  # extra not installed
        raise ExtractorUnavailable(_INSTALL_HINT) from e

    pipeline_options = VlmPipelineOptions(vlm_options=_resolve_vlm_options(vlm_model))
    # REFERENCED export needs rendered picture images; enable whichever toggles
    # this docling build exposes (defensive — VLM options vary across versions).
    for attr in ("generate_picture_images", "generate_page_images"):
        if hasattr(pipeline_options, attr):
            setattr(pipeline_options, attr, True)

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline, pipeline_options=pipeline_options
            )
        }
    )

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
        "vlm_model": vlm_model,
        "image_export_mode": "referenced",
    }
