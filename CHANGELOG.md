# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
