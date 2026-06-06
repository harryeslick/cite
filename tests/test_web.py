"""Tests for the URL citation path (`cite add-url`) and its web helpers.

Parsing and normalization are pure and tested directly. The end-to-end command is
exercised offline by monkeypatching ``cite.web.fetch_url`` — the one network call —
so the fetch/snapshot/parse/commit wiring is covered without touching the network.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cite import web
from cite.cli import app
from cite.dedup import find_url_duplicate, normalize_url
from cite.web import Fetched, parse_webpage_metadata

runner = CliRunner()


def _run(args, input=None):
    result = runner.invoke(app, args, input=input)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _html_fetched(html: str, url: str, content_type: str = "text/html") -> Fetched:
    return Fetched(final_url=url, content_type=content_type, body=html.encode(), text=html)


def _today_parts():
    now = datetime.now(timezone.utc)
    return [now.year, now.month, now.day]


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


def test_parse_prefers_open_graph():
    html = """
    <html><head>
      <title>Fallback Title</title>
      <meta property="og:title" content="OG Title">
      <meta property="og:url" content="https://example.com/canonical">
      <meta property="og:site_name" content="Example News">
      <meta name="author" content="Jane Doe">
      <meta property="article:published_time" content="2023-04-05T10:00:00Z">
    </head><body></body></html>
    """
    rec = parse_webpage_metadata(html, "https://example.com/fetched")
    assert rec["type"] == "webpage"
    assert rec["title"] == "OG Title"
    assert rec["URL"] == "https://example.com/canonical"  # og:url beats fetched url
    assert rec["container-title"] == "Example News"
    assert rec["author"] == [{"literal": "Jane Doe"}]
    assert rec["issued"] == {"date-parts": [[2023, 4, 5]]}


def test_parse_jsonld_only_with_graph():
    html = """
    <html><head><script type="application/ld+json">
      {"@graph":[{"@type":"Article","headline":"LD Headline",
                  "author":{"name":"Org Inc"},"datePublished":"2019"}]}
    </script></head></html>
    """
    rec = parse_webpage_metadata(html, "https://x.test/p")
    assert rec["title"] == "LD Headline"
    assert rec["URL"] == "https://x.test/p"  # no declared URL -> fetched url
    assert rec["author"] == [{"literal": "Org Inc"}]
    assert rec["issued"] == {"date-parts": [[2019]]}  # year-only date


def test_parse_bare_title_fallback():
    rec = parse_webpage_metadata("<html><head><title>Only Title</title></head></html>", "https://y.test/")
    assert rec["title"] == "Only Title"
    assert rec["URL"] == "https://y.test/"
    assert "author" not in rec and "issued" not in rec


def test_parse_no_title_omits_field():
    rec = parse_webpage_metadata("<html><head></head><body>hi</body></html>", "https://z.test/page")
    assert "title" not in rec
    assert rec["URL"] == "https://z.test/page"


# --------------------------------------------------------------------------- #
# normalize_url / find_url_duplicate
# --------------------------------------------------------------------------- #


def test_normalize_url_strips_query_fragment_and_trailing_slash():
    base = "https://example.com/article"
    assert normalize_url("https://example.com/article/") == base
    assert normalize_url("https://example.com/article#section") == base
    assert normalize_url("https://example.com/article?utm_source=x") == base
    # scheme + host lowercased; query that distinguishes pages is (deliberately) dropped
    assert normalize_url("HTTPS://Example.COM/article?id=2") == base
    assert normalize_url(None) is None
    assert normalize_url("  ") is None


def test_find_url_duplicate_exact_only():
    existing = [{"URL": "https://example.com/a", "_provenance": {"new_filename": "x.html"}}]
    assert find_url_duplicate({"URL": "https://example.com/a?ref=1"}, existing)  # same page
    assert not find_url_duplicate({"URL": "https://example.com/b"}, existing)  # different page
    assert not find_url_duplicate({}, existing)  # no URL -> no-op
    match = find_url_duplicate({"URL": "https://example.com/a"}, existing)[0]
    assert match.tier == "definitive" and match.matched_on == ["url"]


# --------------------------------------------------------------------------- #
# add-url end to end (monkeypatched fetch)
# --------------------------------------------------------------------------- #

_PAGE = """
<html><head>
  <title>Climate Report 2023</title>
  <meta property="og:site_name" content="Example Org">
  <meta name="author" content="Example Org">
  <meta property="article:published_time" content="2023-06-01">
</head><body>...</body></html>
"""


def test_add_url_html_snapshots_and_commits(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    url = "https://example.org/climate"
    monkeypatch.setattr(web, "fetch_url", lambda u: _html_fetched(_PAGE, url))

    out = _run(["add-url", url, "--library", str(lib)])
    assert out["status"] == "added"
    assert out["cite_type"] == "web-site"
    rec = out["record"]
    assert rec["type"] == "webpage"
    assert rec["title"] == "Climate Report 2023"
    assert rec["URL"] == url
    assert rec["accessed"] == {"date-parts": [_today_parts()]}
    prov = rec["_provenance"]
    assert prov["source"] == "web"
    assert prov["source_id"] == url

    # The fetched bytes are archived as the bundle's <id>.html snapshot.
    assert out["new_filename"].endswith(".html")
    stem = out["new_filename"].rsplit(".", 1)[0]
    snapshot = lib / stem / out["new_filename"]
    assert snapshot.exists()
    assert snapshot.read_bytes() == _PAGE.encode()


def test_add_url_missing_title_returns_missing_fields(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    monkeypatch.setattr(
        web, "fetch_url",
        lambda u: _html_fetched("<html><head></head><body>x</body></html>", "https://no-title.test/p"),
    )
    out = _run(["add-url", "https://no-title.test/p", "--library", str(lib)])
    assert out["status"] == "missing_fields"
    assert "title" in out["missing"]


def test_add_url_dedup_is_exact_url_only(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    url = "https://example.org/climate"

    # First add succeeds.
    monkeypatch.setattr(web, "fetch_url", lambda u: _html_fetched(_PAGE, url))
    assert _run(["add-url", url, "--library", str(lib)])["status"] == "added"

    # Same URL, *different bytes* (so the exact-hash gate doesn't pre-empt the URL
    # gate) -> near_duplicate on the url. A query arg must not change the verdict.
    variant = _PAGE + "<!-- changed -->"
    monkeypatch.setattr(web, "fetch_url", lambda u: _html_fetched(variant, url + "?utm=ad"))
    dup = _run(["add-url", url + "?utm=ad", "--library", str(lib)])
    assert dup["status"] == "near_duplicate"
    assert dup["candidates"][0]["matched_on"] == ["url"]

    # --force commits past the URL gate.
    assert _run(["add-url", url + "?utm=ad", "--force", "--library", str(lib)])["status"] == "added"

    # A genuinely different URL is a new source, no questions asked. (Distinct
    # bytes too, so this isolates the URL gate from the identical-bytes gate.)
    other = "https://example.org/weather"
    other_page = _PAGE.replace("Climate Report 2023", "Weather Report 2023")
    monkeypatch.setattr(web, "fetch_url", lambda u: _html_fetched(other_page, other))
    assert _run(["add-url", other, "--library", str(lib)])["status"] == "added"


# --------------------------------------------------------------------------- #
# PDF branch + error envelopes
# --------------------------------------------------------------------------- #


def test_add_url_pdf_downloads_and_peeks(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    pdf_bytes = b"%PDF-1.4 fake pdf body"
    monkeypatch.setattr(
        web, "fetch_url",
        lambda u: Fetched("https://example.org/paper.pdf", "application/pdf", pdf_bytes, ""),
    )
    out = _run(["add-url", "https://example.org/paper.pdf", "--library", str(lib)])
    assert out["status"] == "downloaded"
    assert out["kind"] == "pdf"
    assert out["source_url"] == "https://example.org/paper.pdf"
    assert "peek" in out
    saved = Path(out["path"])
    assert saved.exists() and saved.read_bytes() == pdf_bytes
    assert saved.parent.name == ".downloads"
    # Nothing was filed as a record.
    assert _run(["list", "--library", str(lib)])["count"] == 0


def test_add_url_pdf_detected_by_magic_bytes(tmp_path, monkeypatch):
    """A PDF mislabeled as octet-stream is still routed to the PDF branch."""
    lib = tmp_path / "lib"
    monkeypatch.setattr(
        web, "fetch_url",
        lambda u: Fetched("https://x.test/d", "application/octet-stream", b"%PDF-1.7 x", ""),
    )
    out = _run(["add-url", "https://x.test/d", "--library", str(lib)])
    assert out["status"] == "downloaded"


def test_add_url_unsupported_content_type(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    monkeypatch.setattr(
        web, "fetch_url",
        lambda u: Fetched("https://x.test/img", "image/png", b"\x89PNG\r\n", ""),
    )
    out = _run(["add-url", "https://x.test/img", "--library", str(lib)])
    assert out["status"] == "unsupported_content_type"
    assert out["content_type"] == "image/png"


def test_add_url_fetch_failure_returns_clean_envelope(tmp_path, monkeypatch):
    lib = tmp_path / "lib"

    def _boom(u):
        raise web.WebFetchError("could not fetch https://dead.test/: timeout")

    monkeypatch.setattr(web, "fetch_url", _boom)
    out = _run(["add-url", "https://dead.test/", "--library", str(lib)])
    assert out["status"] == "fetch_failed"
    assert "timeout" in out["message"]


# --------------------------------------------------------------------------- #
# fetch_url: curl_cffi mapping + error handling (offline, fake response)
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, status_code, headers, content, text, url):
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.text = text
        self.url = url


def test_fetch_url_maps_response_fields(monkeypatch):
    fake = _FakeResp(
        200, {"content-type": "text/html; charset=UTF-8"}, b"<html>x</html>", "<html>x</html>",
        "https://example.com/landed",
    )
    captured = {}

    def _get(url, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(web.cffi, "get", _get)
    fetched = web.fetch_url("https://example.com")
    # Impersonation + redirect-following are requested.
    assert captured["impersonate"] == web._IMPERSONATE
    assert captured["allow_redirects"] is True
    # content-type is normalized (params stripped, lowercased); fields mapped through.
    assert fetched.content_type == "text/html"
    assert fetched.final_url == "https://example.com/landed"
    assert fetched.body == b"<html>x</html>"


def test_fetch_url_non_success_raises(monkeypatch):
    monkeypatch.setattr(
        web.cffi, "get",
        lambda url, **kw: _FakeResp(403, {}, b"", "", url),
    )
    try:
        web.fetch_url("https://blocked.test/")
        assert False, "expected WebFetchError"
    except web.WebFetchError as exc:
        assert "403" in exc.message


def test_fetch_url_network_error_raises(monkeypatch):
    def _boom(url, **kw):
        raise web.RequestException("dns boom")

    monkeypatch.setattr(web.cffi, "get", _boom)
    try:
        web.fetch_url("https://dead.test/")
        assert False, "expected WebFetchError"
    except web.WebFetchError as exc:
        assert "could not fetch" in exc.message
