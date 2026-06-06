# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-06

### Added

- `cite prepare <file>` — pre-add full-markdown extraction for better citation
  context, especially for reports with no DOI and thin embedded metadata. Runs
  the local Docling VLM once, caches the markdown by content hash under
  `<library>/.staging/<hash>/`, and returns the markdown head plus any DOI/title
  read from the extracted text. Degrades cleanly to `status: extractor_unavailable`
  (redirecting to `cite peek`) when the optional `extract` extra is absent.
  Exposed as the `prepare` MCP tool.
- `cite version` now reports an `extras` map (`{"mcp": bool, "extract": bool}`)
  so callers can tell which optional features are installed without invoking them.

### Changed

- `cite add` adopts a staged extraction for matching bytes: it moves the cached
  markdown + image artifacts into the reference bundle (rewriting image links to
  the id stem) and records `_provenance.extraction`, so the expensive VLM pass
  runs exactly once across `prepare` → `add`. The `added` envelope now reports
  `extracted` and `markdown_path`.
- Agent guide and MCP instructions now describe a prepare-first workflow when the
  `extract` extra is available, with `cite peek` as the deterministic fallback.
