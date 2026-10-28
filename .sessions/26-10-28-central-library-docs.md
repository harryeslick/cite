# Session: 26-10-28 — central-library-docs

## Scope
Status review of v0.4.0, then audit of `docs/plans/2026-06-24-001-feat-central-library-plan.md`.
Found U1–U6 implemented and tested, but the plan's deferred docs never written, a duplicate
`ops.pull`, and README text still listing the feature as future work. Fixed those (A4–A6).

## Key Changes
- `src/cite/ops.py`: removed the first (shadowed, dead) `def pull`; the live one is unchanged and returns no `record`.
- `src/cite/guide.py`: new `pull` command entry; `--central` noted on `list` and `remove`.
- `src/cite/mcp.py`: `_INSTRUCTIONS` paragraph on central sync, `source: "central"`, `list_refs(central=True)` + `pull(id)`.
- `AGENTS.md`: new section "Central library (`~/.cite/`)".
- `README.md`: new "The central library (`~/.cite/`)" subsection; "One central library" (`$CITE_LIBRARY`) renamed "One shared library"; commands table gains `pull`, `list --central`, `remove --central`; roadmap item removed.
- `CHANGELOG.md`: new `[Unreleased]` section (Changed + Removed).

## Tests Run
- `uv run pytest -q`: 257 passed, 1 failed. The failure is `test_version_reports_package_version` (`'0.3.0' == '0.4.0'`), caused by stale `.venv` package metadata; it was failing before these changes.
- `cite guide --json` lists `pull`; `cite.mcp._INSTRUCTIONS` contains "central".

## Open Items
- A1: run `uv sync` to refresh the venv metadata and clear the version test.
- (done) `skills/cite/SKILL.md`: new section "The central library (`~/.cite/`) remembers every add"; description mentions cross-project reuse.
- A2: no git tags exist for releases.
- Changes are not committed.
