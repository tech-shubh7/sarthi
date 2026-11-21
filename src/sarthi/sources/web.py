"""Shared plumbing for sources that read public JSON or RSS over HTTP: errors, size limits, plain text.

HTTP failures are turned into the queue's error kinds (``RetryableError`` for timeouts, 429 and 5xx;
``PermanentError`` for a feed that no longer exists), so every source behaves the same under the scheduler.
Text from feeds is written by strangers: ``plain_text`` keeps only words (port of StillOpen's ``hn_plain``,
Apache-2.0), and is never shown as markup.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from sarthi import __version__
from sarthi.queue.tasks import PermanentError, RetryableError

USER_AGENT = f"Sarthi/{__version__} (self-hosted job search)"
TIMEOUT_S = 30.0
MAX_BYTES = 8_000_000  # one answer bigger than this is refused, not parsed
TEXT_CHARS = 12_000
LINE_CHARS = 400

_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500
_OK = 200

_DROP_BLOCKS = re.compile(r"<\s*(script|style)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_BREAKS = re.compile(r"<\s*(?:p|br|pre|li|div|h[1-6]|tr)\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]*>")
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]")
_SPACES = re.compile("[ \t\u00a0]+")


async def resolve_host(host: str, port: int) -> list[str]:
    """Every IP address ``host`` resolves to (replaced in tests)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_public_url(url: str) -> None:
    """Refuse a link that is not a public web address (the SSRF guard, docs/STANDARDS.md §4).

    Only http(s), no login inside the link, and every address the host resolves to must be public: never
    this computer, the home network, or cloud metadata. Raises ``PermanentError`` (or ``RetryableError``
    when the name cannot be looked up right now).
    """
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise PermanentError("not a valid link") from error
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PermanentError("only http and https links can be watched")
    if parts.username or parts.password:
        raise PermanentError("links with a login inside them are not allowed")
    try:
        addresses = await resolve_host(parts.hostname, port)
    except OSError as error:
        raise RetryableError(f"{parts.hostname}: name not found") from error
    for address in addresses:
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            raise PermanentError(f"{parts.hostname} points to a private or local address")


async def _public_only(request: httpx.Request) -> None:
    await check_public_url(str(request.url))


def client(
    transport: httpx.AsyncBaseTransport | None = None, *, public_only: bool = False
) -> httpx.AsyncClient:
    """An HTTP client for public feeds, short timeout.

    ``public_only`` checks every request, redirects included, with ``check_public_url``: use it for any link a
    person or a post supplied.
    """
    return httpx.AsyncClient(
        timeout=TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, */*"},
        transport=transport,
        event_hooks={"request": [_public_only]} if public_only else None,
    )


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "")
    return float(value) if value.isdigit() else None


async def fetch(http: httpx.AsyncClient, name: str, url: str, **params: str) -> httpx.Response:
    """GET ``url``; raises ``RetryableError`` / ``PermanentError`` on failure, never returns a bad answer."""
    try:
        response = await http.get(url, params=params or None)
    except (httpx.TransportError, TimeoutError) as error:
        raise RetryableError(f"{name}: {type(error).__name__}") from error
    status = response.status_code
    if status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
        raise RetryableError(f"{name}: HTTP {status}", retry_after=_retry_after(response), status=status)
    if status != _OK:
        raise PermanentError(f"{name}: HTTP {status}")
    if len(response.content) > MAX_BYTES:
        raise RetryableError(f"{name}: answer too large ({len(response.content)} bytes)")
    return response


async def fetch_json(http: httpx.AsyncClient, name: str, url: str, **params: str) -> Any:
    """GET ``url`` and decode JSON; a non-JSON answer is a temporary failure (sites send error pages)."""
    response = await fetch(http, name, url, **params)
    try:
        return response.json()
    except ValueError as error:
        raise RetryableError(f"{name}: answer is not JSON") from error


_MOJIBAKE = re.compile(
    "[\u00c2\u00c3\u00c5\u00d8\u00d9\u00e2][\u0080-\u00bf\u2010-\u203a\u20ac\u0152-\u0178]"
)


def fix_mojibake(text: str) -> str:
    """Undo UTF-8 text that was decoded as Latin-1/Windows-1252 somewhere upstream ("MÃ¼nchen" -> "München").

    Only applied when the result decodes cleanly, so real accented text is never damaged.
    """
    if not _MOJIBAKE.search(text):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            return text.encode(codec).decode("utf-8")
        except UnicodeError:
            continue
    return text


def plain_text(raw: str | None, limit: int = TEXT_CHARS) -> str:
    """Readable plain text from HTML written by anyone.

    Scripts and styles go with their contents; every tag is removed twice (before and after entities are
    undone, so escaped markup cannot come back as tags); control and direction-override characters go;
    each line is cut to a sane length.
    """
    text = _DROP_BLOCKS.sub(" ", raw or "")
    text = _BREAKS.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = html.unescape(text)
    text = _ANY_TAG.sub("", _DROP_BLOCKS.sub(" ", text))
    text = _CONTROL.sub("", fix_mojibake(text))
    lines = [_SPACES.sub(" ", line).strip()[:LINE_CHARS] for line in text.split("\n")]
    return "\n".join(line for line in lines if line)[:limit]


def parse_time(value: Any) -> datetime | None:
    """A posting time from the shapes feeds use: epoch seconds, ISO 8601 (with or without zone), RFC 822."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return parse_time(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
