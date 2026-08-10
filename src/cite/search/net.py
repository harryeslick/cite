"""Shared HTTP plumbing and caller identity for the search backends.

Two jobs live here, both of which used to be copy-pasted into each backend:

1. **Telling "not there" apart from "couldn't ask".** Every backend previously
   collapsed timeouts, 429s and 5xx into the same "no record" sentinel, so a
   rate-limited lookup reached the agent as "no match found — type it in by
   hand" and a perfectly indexed paper got hand-keyed. :func:`get` makes that
   mistake unavailable: a transport failure or a non-2xx that isn't 404 raises
   :class:`SearchUnavailable`, and only 2xx/404 come back as a response.

2. **Who we say we are.** CrossRef wants a contact address in the User-Agent;
   OpenAlex now bills against a daily credit budget and grants ten times the
   budget to a request carrying an ``api_key``. Both are read here — from the
   library's ``cite.toml`` or the environment — so no backend hardcodes an
   identity, and absent config all three backends keep working exactly as
   before (SUITE §7 degradation).

Reading ``cite.toml`` from *this* layer is why ``resolve_library`` lives in
``cite.store`` rather than ``cite.ops``: ops imports search, so search cannot
import ops, and duplicating the resolution walk would give the tool two
different answers to "which library".
"""

from __future__ import annotations

import os

import httpx

from cite import __version__
from cite.store import resolve_library

#: Contact address advertised to the APIs. CrossRef asks for one and gives
#: identified traffic a better-behaved pool; it is a courtesy, never required.
CONTACT_EMAIL_ENV = "CITE_CONTACT_EMAIL"
#: OpenAlex API key. Free, and worth having: $1/day of credit versus $0.10/day
#: unauthenticated (developers.openalex.org/guides/authentication).
OPENALEX_API_KEY_ENV = "OPENALEX_API_KEY"

#: The ``cite.toml`` table both settings live under, keyed by setting name.
CONFIG_SECTION = "search"
CONTACT_EMAIL_KEY = "contact_email"
OPENALEX_API_KEY_KEY = "openalex_api_key"

_PROJECT_URL = "https://github.com/hfsi/cite"

# Failure kinds, ordered from most to least actionable for the caller.
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
NETWORK_ERROR = "network_error"
BAD_RESPONSE = "bad_response"

_TIMEOUT = 15.0


class SearchUnavailable(Exception):
    """A backend could not be asked — distinct from asking and finding nothing.

    Raised for rate limits, server errors, transport failures and unparseable
    bodies. Never raised for a genuine 404 or an empty result set: those are
    answers, and callers return them as ``not_found``.
    """

    def __init__(
        self,
        source: str,
        kind: str,
        reason: str,
        *,
        retry_after: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.kind = kind
        self.reason = reason
        self.retry_after = retry_after
        self.status_code = status_code

    def as_dict(self) -> dict:
        """Compact envelope entry — one short line per unreachable source."""
        entry: dict = {
            "source": self.source,
            "kind": self.kind,
            "reason": self.reason,
        }
        if self.retry_after is not None:
            entry["retry_after"] = self.retry_after
        return entry


def _setting(env_var: str, config_key: str) -> tuple[str | None, str | None]:
    """Resolve one optional setting; return ``(value, where_it_came_from)``.

    Environment first, then the library's ``cite.toml`` ``[search]`` table. The
    environment wins so a one-off ``OPENALEX_API_KEY=... cite search`` overrides
    the file, and because that is the precedence every other tool has trained
    people to expect. ``where_it_came_from`` exists for ``cite doctor --self``:
    with two possible sources, "it's set" is a much less useful answer than
    "it's set, from here".

    The library is resolved the same way every command resolves it, so a
    per-project library carries its own identity and an MCP host needs no
    per-server environment wiring.
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        return value, "env"

    table = resolve_library(None).config().get(CONFIG_SECTION) or {}
    raw = table.get(config_key)
    value = raw.strip() if isinstance(raw, str) else ""
    return (value, "cite.toml") if value else (None, None)


def contact_email() -> str | None:
    """The configured contact address, or None if nothing has set one."""
    return _setting(CONTACT_EMAIL_ENV, CONTACT_EMAIL_KEY)[0]


def user_agent() -> str:
    """User-Agent for outbound API calls, with a mailto when one is configured.

    CrossRef's convention is ``product/version (mailto:...)``; without a
    configured address we identify the project rather than inventing a person.
    """
    email = contact_email()
    suffix = f"mailto:{email}" if email else f"+{_PROJECT_URL}"
    return f"cite/{__version__} ({suffix})"


def openalex_api_key() -> str | None:
    """The configured OpenAlex API key, or None (unauthenticated still works)."""
    return _setting(OPENALEX_API_KEY_ENV, OPENALEX_API_KEY_KEY)[0]


def identity_report() -> dict:
    """What identity the next request would carry, and where it was configured.

    For ``cite doctor --self``. The API key's *value* is never returned — the
    question a health check answers is whether one is set, and echoing a secret
    into an agent's context is a cost with no benefit.
    """
    email, email_from = _setting(CONTACT_EMAIL_ENV, CONTACT_EMAIL_KEY)
    key, key_from = _setting(OPENALEX_API_KEY_ENV, OPENALEX_API_KEY_KEY)
    return {
        "contact_email": email,
        "contact_email_source": email_from,
        "openalex_api_key": key is not None,
        "openalex_api_key_source": key_from,
        "user_agent": user_agent(),
        "configure": (
            f"[{CONFIG_SECTION}] in the library's cite.toml, "
            f"or ${CONTACT_EMAIL_ENV} / ${OPENALEX_API_KEY_ENV}"
        ),
    }


def _retry_after(response: httpx.Response) -> int | None:
    """Seconds to wait, from the Retry-After header. None if absent or a date.

    Only the delta-seconds form is honoured: an HTTP-date is rare here and a
    wrong number is worse than no number, so we say nothing rather than guess.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def get(
    url: str,
    *,
    source: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    """GET ``url``, raising :class:`SearchUnavailable` for anything unanswerable.

    Returns the response only for 2xx and 404 — the two outcomes that are real
    answers. Sending the User-Agent on every call is deliberate: it costs
    nothing and it is the identification CrossRef asks for.
    """
    request_headers = {"User-Agent": user_agent()}
    if headers:
        request_headers.update(headers)

    try:
        response = httpx.get(
            url,
            params=params,
            headers=request_headers,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise SearchUnavailable(
            source, NETWORK_ERROR, f"could not reach {source}: {exc.__class__.__name__}"
        ) from exc

    if response.is_success or response.status_code == 404:
        return response

    if response.status_code == 429:
        raise SearchUnavailable(
            source,
            RATE_LIMITED,
            f"{source} rate limit reached",
            retry_after=_retry_after(response),
            status_code=429,
        )

    raise SearchUnavailable(
        source,
        SERVER_ERROR,
        f"{source} returned HTTP {response.status_code}",
        retry_after=_retry_after(response),
        status_code=response.status_code,
    )


def parse_json(response: httpx.Response, source: str) -> dict:
    """Decode a JSON body, treating a malformed one as "couldn't ask"."""
    try:
        data = response.json()
    except Exception as exc:
        raise SearchUnavailable(
            source, BAD_RESPONSE, f"{source} returned an unreadable body"
        ) from exc
    if not isinstance(data, dict):
        raise SearchUnavailable(
            source, BAD_RESPONSE, f"{source} returned an unexpected payload shape"
        )
    return data
