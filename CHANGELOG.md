# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-29

### Added

- **Extraction engine selection.** `cite extract` / `cite prepare` gained `--engine auto|text|vlm`, defaulting to `auto`. `src/cite/extract/probe.py` reads the document's text layer with pypdf (already core — no new dependency, milliseconds, no ML) and reports `{pages, sampled, median_chars_per_page, pages_without_text, verdict}`. On `auto`, a `text` verdict routes to Docling's `StandardPdfPipeline` (layout + table-structure models over the PDF's own text) and a `scanned` verdict — including any unreadable or non-PDF input, where the VLM is the safe fallback — routes to the existing `VlmPipeline`. A 181-page born-digital book now extracts in ~45 s instead of tens of minutes, and the text engine cannot misread text that is already in the file. The engine used and the probe that chose it are returned in the envelope and stored in `_provenance.extraction`.
- `cite doctor --self` (and the `doctor(self_check=True)` MCP tool): checks the *install* rather than the library's contents — which optional extras are present, whether the `cite-mcp` entrypoint can start, and which library root resolved and how. A registered-but-unstartable `cite-mcp` (the `mcp` extra missing) is invisible to the host that launched it; this reports it in one call.
- `Extraction.engine` and `Extraction.probe` fields on the provenance model. Both optional, so records written before the engine split still load.
- Tests: `tests/test_probe.py` (a hand-built minimal PDF exercises the born-digital, no-text-layer, unreadable, and sampling-cap paths), library-resolution cases in `tests/test_store.py`, and self-check cases in `tests/test_doctor.py`.

### Changed

- **Library resolution now walks up for `cite.toml`.** With no `--library` and no `$CITE_LIBRARY`, `ops.resolve_library` searches from the current directory upwards, testing each ancestor both as a library and as the parent of a `library/`. An explicitly named path is still used literally and never searched from. `Library` carries `origin` and `searched_from` so errors can explain how the root was chosen.
- The text engine enables OCR only when the probe actually saw pages without text. Leaving `do_ocr` on unconditionally cost a ~25 MB RapidOCR model download on first run and an engine init on every run, to read pages already known not to exist (33.8 s → 5.2 s on a small born-digital PDF).
- `guide`, `AGENTS.md`, `skills/cite/SKILL.md`, and `README.md` document the engine choice, the resolution rules, `doctor --self`, and — new — that `search` and `validate` are library-independent and reject `--library`, and that `validate` checks a CSL record on stdin rather than a stored one.
- The identify step now leads with `cite peek` and reserves `cite prepare` for files whose embedded metadata is thin, rather than recommending full extraction first.
- `_EXTRA_PROBES` / `installed_extras()` moved from `cli.py` to `doctor.py` so both shells can use them.
- README install instructions lead with a single global install from the git remote — `uv tool install --force --from git+https://github.com/harryeslick/cite.git 'cite[all]'` — which is also the upgrade path (`--force` re-pulls `main`, where a git source has no version for uv to compare). The local-checkout form is kept as the development alternative, and the extraction section no longer documents a separate `cite[extract]` global install now that `[all]` covers it.

### Fixed

- **A missing library no longer reports as a healthy empty one.** `doctor`, `list`, `get`, `text`, and `export` (CLI and MCP) now call `require_initialized`, which returns `status: no_library` and exits non-zero instead of `{"status": "ok", "checked": 0}` / `{"count": 0}`. Previously `cd library && cite doctor` resolved `library/library`, found nothing, and reported a healthy empty library — making "you are pointed at the wrong directory" indistinguishable from "nothing is filed yet". The envelope names the resolved path, how it was chosen, and where the search began.
- `require_initialized`'s envelope changed from `status: error` to the more specific `status: no_library`.

## [0.2.3] - 2026-06-08

### Added

- `cite doctor` CLI command and `doctor` MCP tool: a read-only health check over the whole library (roadmap item #2). Walks every bundle and flags `invalid_json` (unparseable record), `missing_provenance` (no `_provenance.cite_type`), `missing_fields` (fails its type's required-field check), `missing_file` (the `_provenance.new_filename` source document is absent), and `orphan` (a bundle directory with no record JSON). Returns a context-frugal envelope — `status` (`ok`/`problems`), `checked`/`healthy`/`problem_count` counts, a per-class `summary` histogram, and one short line per problem — never the full records.
- `src/cite/doctor.py`: pure `run_doctor(lib)` engine. Walks the filesystem directly (not `list_records()`, which silently skips unparseable bundles — the very corruption the check must surface), reusing `models.missing_required_fields` and the `cite.toml` / `.staging/` skip rules.

### Changed

- README roadmap: removed the two now-shipped items — near-duplicate detection (already implemented in `dedup.py`) and the library health check (this change). Documented `cite doctor` in the Commands table.

## [0.2.2] - 2026-06-07

### Added

- `cite update <id>` CLI command and `update` MCP tool: amend a stored reference's metadata in place — the missing CRUD verb between `add` and `remove`. `--field key=value` sets/replaces (same mini-language as `add --manual`), `--remove-field key` deletes a CSL key, `--type` changes the cite_type (rewriting CSL `type`/`genre` and `_provenance.cite_type`). The patch is re-validated against the type; on `missing_fields` nothing is committed. The attached document, its content hash, and provenance are preserved.
- `Library.rename_bundle(old_id, new_id)` in `store.py`: re-stems a whole reference bundle (directory + each contained file stem + markdown image links) when an id-bearing edit (title / author / year) changes the deterministic id. The content-hash suffix is stable, so dedup identity survives the rename. Raises `FileExistsError` on an id clash. Returns the document's new filename so `update` can refresh `_provenance.new_filename`.

### Changed

- Extracted the `--field key=value` parser from `_record_from_fields` into a shared `_apply_fields()` helper, reused by both `add --manual` and `update --field`. No behaviour change to `add`.
- `cite guide` documents the new `update` command.

### Fixed

- `cite version` test went red right after the 0.2.1 bump because the *installed* distribution metadata was stale; `uv pip install -e .` rebuilds the dynamic version. (Process note: `uv sync` alone does not refresh it.)

## [0.2.1] - 2026-06-06

### Added

- `cite add-url <url>` CLI command and `add_url` MCP tool: add a citation from a bare URL. Branches on response Content-Type:
  - **HTML** → archives an HTML snapshot as the reference's document and parses Open Graph / `<title>` / JSON-LD metadata into a `web-site` record (with `accessed` set to today).
  - **PDF** → downloads to `<library>/.downloads/` and `peek`s it, returning `status: downloaded` so the agent continues with the normal `search` → `add` flow.
- `src/cite/web.py`: `fetch_url`, `parse_webpage_metadata`, and content-type helpers (depends on new `selectolax` dependency for HTML parsing).
- Web pages fetched with `curl_cffi` impersonating Chrome (new core dependency) instead of `httpx`, to bypass anti-bot detection like Cloudflare.
- URL deduplication: `dedup.normalize_url` (strips query string + fragment, lowercases scheme/host) and `dedup.find_url_duplicate`.

### Changed

- Extracted the file-agnostic commit pipeline from `cli.add()` into a shared `_commit_file()` helper (validate → dedup → hash → store → provenance), reused by both `add` and `add-url`.
- `Provenance.source` now includes `"web"`.
- `cite guide` documents `add-url`, the web workflow, and the exact-URL dedup rule.

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
