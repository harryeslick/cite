# Agent Session Log

Run `/version-increment` to consolidate into the project CHANGELOG.md.

---

## 2026-06-06 — Add citations from a URL (`cite add-url`)

### Added
- `cite add-url <url>` CLI command and `add_url` MCP tool: add a citation from a
  bare URL. Branches on the response Content-Type:
  - **HTML** → archives an HTML snapshot as the reference's document and parses
    Open Graph / `<title>` / JSON-LD metadata into a `web-site` record (with
    `accessed` set to today).
  - **PDF** (by Content-Type or a `%PDF` magic-byte sniff) → downloads to
    `<library>/.downloads/` and `peek`s it, returning `status: downloaded` so the
    agent continues with the normal `search` → `add` document flow.
- `src/cite/web.py`: `fetch_url`, `parse_webpage_metadata`, and content-type
  helpers (depends on new `selectolax` dependency for HTML parsing).
- Web pages are fetched with `curl_cffi` impersonating Chrome (new core
  dependency) instead of `httpx`. Anti-bot front-ends like Cloudflare fingerprint
  the TLS/HTTP2 handshake and 403 the Python HTTP stack regardless of headers;
  a real-Chrome fingerprint clears those blocks. (The DOI/search API backends
  keep using `httpx` with cite's mailto User-Agent.)
- URL deduplication: `dedup.normalize_url` (strips query string + fragment,
  lowercases scheme/host) and `dedup.find_url_duplicate` — the web path rejects
  only an exact same-URL match; everything else is a new source.

### Changed
- Extracted the file-agnostic commit pipeline from `cli.add()` into a shared
  `_commit_file()` helper (validate → dedup → hash → store → provenance), reused
  by both `add` and `add-url`. No behaviour change to `add`.
- `Provenance.source` now includes `"web"`.
- `cite guide` documents `add-url`, the web workflow, and the exact-URL dedup rule.
