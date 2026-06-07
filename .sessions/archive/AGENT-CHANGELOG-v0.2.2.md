# Agent Session Log

Run `/version-increment` to consolidate into the project CHANGELOG.md.

---

## 2026-06-07 — Edit stored references in place (`cite update`)

### Added
- `cite update <id>` CLI command and `update` MCP tool: amend a stored
  reference's metadata in place — the missing CRUD verb between `add` and
  `remove`. `--field key=value` sets/replaces (same mini-language as
  `add --manual`), `--remove-field key` deletes a CSL key, `--type` changes the
  cite_type (rewriting CSL `type`/`genre` and `_provenance.cite_type`). The patch
  is re-validated against the type; on `missing_fields` nothing is committed. The
  attached document, its content hash, and provenance are preserved.
- `Library.rename_bundle(old_id, new_id)` in `store.py`: re-stems a whole
  reference bundle (directory + each contained file stem + markdown image links)
  when an id-bearing edit (title / author / year) changes the deterministic id.
  The content-hash suffix is stable, so dedup identity survives the rename.
  Raises `FileExistsError` on an id clash. Returns the document's new filename so
  `update` can refresh `_provenance.new_filename`.

### Changed
- Extracted the `--field key=value` parser from `_record_from_fields` into a
  shared `_apply_fields()` helper, reused by both `add --manual` and
  `update --field`. No behaviour change to `add`.
- `cite guide` documents the new `update` command.

### Fixed
- `cite version` test went red right after the 0.2.1 bump because the *installed*
  distribution metadata was stale; `uv pip install -e .` rebuilds the dynamic
  version. (Process note: `uv sync` alone does not refresh it.)
