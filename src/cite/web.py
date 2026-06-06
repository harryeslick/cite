"""Fetch a web resource and (for HTML) read its bibliographic metadata.

This is the ``search/`` layer's sibling for the *web* path: it performs the one
network call ``cite add-url`` needs and turns an HTML page into a CSL ``webpage``
record. The split mirrors the rest of the package — I/O + parsing live here, the
CLI stays thin glue.

Determinism caveat: a live page is not reproducible the way a DOI lookup is (its
markup changes), but the *parse* is pure — same HTML in, same record out — and we
only ever read declared metadata (Open Graph / ``<title>`` / JSON-LD), never infer
or fabricate. Anything we can't read is simply absent, and the validator then asks
the user for it.
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple

from curl_cffi import requests as cffi
from curl_cffi.requests.exceptions import RequestException
from selectolax.lexbor import LexborHTMLParser

# We fetch web pages with curl_cffi impersonating a real Chrome rather than httpx
# (which the DOI/search *API* backends use with cite's mailto User-Agent). The
# reason is TLS: anti-bot front-ends like Cloudflare fingerprint the TLS/HTTP2
# handshake, and the Python HTTP stack's fingerprint is flagged as a bot — so a
# plain GET returns 403 no matter what headers it sends. curl_cffi replicates
# Chrome's exact TLS + HTTP2 fingerprint, so a public page a reader could open in
# a browser loads for us too. `impersonate="chrome"` tracks the latest preset.
_IMPERSONATE = "chrome"
_TIMEOUT = 20.0

# Leading YYYY[-MM[-DD]] of an ISO-8601-ish date string (e.g. "2023-04-05T10:00Z").
_ISO_DATE = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")


class WebFetchError(Exception):
    """A URL could not be fetched (network error, timeout, or HTTP error status).

    Carries a one-line ``message`` suitable for a clean error envelope — the CLI
    surfaces it without a traceback (see SUITE context-frugal output rules).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Fetched(NamedTuple):
    """The result of a successful fetch.

    ``body`` is the exact bytes the server sent (archived verbatim as the
    snapshot); ``text`` is those bytes decoded for parsing.
    """

    final_url: str  # URL after redirects
    content_type: str  # normalized, params stripped, lowercased (e.g. "text/html")
    body: bytes
    text: str


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def fetch_url(url: str) -> Fetched:
    """GET ``url`` as a real browser (following redirects) and return a :class:`Fetched`.

    Uses curl_cffi with Chrome impersonation so anti-bot front-ends that fingerprint
    the TLS handshake (e.g. Cloudflare) serve the page instead of a 403. Raises
    :class:`WebFetchError` on any network failure, timeout, or non-success HTTP
    status — the caller turns that into an error envelope.
    """
    try:
        response = cffi.get(
            url,
            impersonate=_IMPERSONATE,
            timeout=_TIMEOUT,
            allow_redirects=True,
        )
    except RequestException as exc:
        raise WebFetchError(f"could not fetch {url}: {exc}") from exc

    if not (200 <= response.status_code < 300):
        raise WebFetchError(f"{url} returned HTTP {response.status_code}")

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return Fetched(
        final_url=str(response.url),
        content_type=content_type,
        body=response.content,
        text=response.text,  # curl_cffi decodes using the response charset
    )


def is_pdf(fetched: Fetched) -> bool:
    """True if the resource is a PDF — by declared type or a ``%PDF`` magic sniff.

    The sniff catches servers that mislabel a PDF as ``application/octet-stream``
    or a generic type, so a PDF URL never slips into the HTML-parsing branch.
    """
    return fetched.content_type == "application/pdf" or fetched.body[:5] == b"%PDF-"


def is_html(fetched: Fetched) -> bool:
    """True if the resource is an HTML (or XHTML) page."""
    return fetched.content_type in ("text/html", "application/xhtml+xml")


# --------------------------------------------------------------------------- #
# Parse HTML -> CSL `webpage` record
# --------------------------------------------------------------------------- #


def _meta(parser: LexborHTMLParser, key: str) -> str | None:
    """Content of ``<meta property=key>`` or ``<meta name=key>`` (first non-empty)."""
    for attr in ("property", "name"):
        node = parser.css_first(f'meta[{attr}="{key}"]')
        if node is not None:
            content = (node.attributes.get("content") or "").strip()
            if content:
                return content
    return None


def _jsonld_objects(parser: LexborHTMLParser) -> list[dict]:
    """All JSON-LD objects on the page, flattened (handles arrays and ``@graph``)."""
    objects: list[dict] = []
    for node in parser.css('script[type="application/ld+json"]'):
        raw = node.text(deep=True, strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                graph = item.get("@graph")
                if isinstance(graph, list):
                    objects.extend(o for o in graph if isinstance(o, dict))
                else:
                    objects.append(item)
    return objects


def _jsonld_value(objects: list[dict], *keys: str):
    """First present value for any of ``keys`` across the JSON-LD objects."""
    for obj in objects:
        for key in keys:
            if obj.get(key):
                return obj[key]
    return None


def _author_name(value) -> str | None:
    """Coerce a JSON-LD/meta author value (str | {name} | list) to a display name."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _author_name(value.get("name"))
    if isinstance(value, list):
        for item in value:
            name = _author_name(item)
            if name:
                return name
    return None


def _date_parts(raw) -> dict:
    """Parse a YYYY[-MM[-DD]] prefix of ``raw`` into a CSL date object ({} if none)."""
    if not isinstance(raw, str):
        return {}
    match = _ISO_DATE.match(raw.strip())
    if not match:
        return {}
    parts = [int(g) for g in match.groups() if g]
    return {"date-parts": [parts]} if parts else {}


def parse_webpage_metadata(text: str, final_url: str) -> dict:
    """Build a CSL ``webpage`` record from an HTML document.

    Reads, in preference order, Open Graph / Twitter ``<meta>`` tags, the
    ``<title>`` element, and JSON-LD. ``accessed`` is always set to today (the only
    field we *assert* rather than read); ``URL`` falls back to the fetched URL.
    Missing fields are simply left out for the validator to flag.
    """
    parser = LexborHTMLParser(text)
    jsonld = _jsonld_objects(parser)

    record: dict = {"type": "webpage"}

    # --- title ---
    title = _meta(parser, "og:title") or _meta(parser, "twitter:title")
    if not title:
        node = parser.css_first("title")
        title = node.text(strip=True) if node is not None else None
    if not title:
        title = _author_name(_jsonld_value(jsonld, "headline", "name"))
    if title:
        record["title"] = title

    # --- URL (prefer the page's declared canonical, else where we landed) ---
    url = _meta(parser, "og:url")
    if not url:
        link = parser.css_first('link[rel="canonical"]')
        url = (link.attributes.get("href") if link is not None else None) or None
    record["URL"] = (url or final_url).strip()

    # --- container (site name) ---
    site = _meta(parser, "og:site_name")
    if site:
        record["container-title"] = site

    # --- author ---
    author = _meta(parser, "author") or _author_name(_jsonld_value(jsonld, "author"))
    if not author:
        author = _meta(parser, "article:author")
    if author:
        record["author"] = [{"literal": author}]

    # --- issued (publication date) ---
    issued_raw = (
        _meta(parser, "article:published_time")
        or _jsonld_value(jsonld, "datePublished", "dateCreated")
        or _meta(parser, "date")
    )
    issued = _date_parts(issued_raw)
    if issued:
        record["issued"] = issued

    return record
