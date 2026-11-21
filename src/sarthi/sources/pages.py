"""Watched pages: hiring-drive pages or any careers page not on a known hiring system.

Pages like TCS NextStep, Infosys, Wipro and Cognizant. Idea and change detection from JobRadar's page
watcher (MIT).

Each read compares the page's lines with the last read, ignoring timestamps, counters and furniture. The first
read only records the page. Enough new text becomes one post, read like a channel post (rules, then AI); a
page that changed almost entirely is a redesign and only becomes the new baseline.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from sarthi.ai.client import AiClient
from sarthi.db.watched import WatchStore
from sarthi.jobs.models import JobPosting
from sarthi.sources import web
from sarthi.sources.base import SearchQuery
from sarthi.sources.posts import MIN_ADDED, absolute_links, added_lines, is_redesign, page_lines, post_to_job

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 60_000
_ANCHOR = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["']([^"'#][^"']*)["'][^>]*>(.*?)</a>""", re.I | re.S)


def page_text(html: str, page_url: str) -> str:
    """Readable lines of a page, links kept as "text (url)" so new lines carry their links."""

    def anchor(m: re.Match[str]) -> str:
        label = web.plain_text(m.group(2), 200).replace("\n", " ")
        url = urljoin(page_url, m.group(1).strip())
        if urlsplit(url).scheme not in ("http", "https"):
            return label  # javascript:, mailto:, tel: ... are not links to a job
        return f"{label} ({url})"

    return web.plain_text(_ANCHOR.sub(anchor, html), MAX_PAGE_CHARS)


class PageSource:
    """New announcements on one page."""

    def __init__(
        self,
        url: str,
        name: str,
        store: WatchStore,
        *,
        ai: AiClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Watch ``url`` (shown to the user as ``name``); the last text is kept in ``store``."""
        self.url = url
        self.label = name or urlsplit(url).hostname or url
        self.store = store
        self.ai = ai
        self._transport = transport

    @property
    def name(self) -> str:
        """``"page:<url>"``."""
        return f"page:{self.url}"

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """The jobs announced since the last read (none on the first read or after a redesign)."""
        async with web.client(self._transport, public_only=True) as http:  # a link a person typed in
            response = await web.fetch(http, self.name, self.url)
        lines = page_lines(page_text(response.text, self.url))
        before = self.store.page_text(self.url)
        self.store.set_page_text(self.url, "\n".join(lines))
        if before is None:
            return []
        added = added_lines(before.splitlines(), lines)
        if is_redesign(lines, added) or sum(len(line) for line in added) < MIN_ADDED:
            return []
        post = f"{self.label}\n" + "\n".join(added)
        posting = await post_to_job(
            post,
            source="page",
            posted_at=None,
            fallback_url=(absolute_links(added, self.url) or [announcement_url(self.url, added)])[0],
            ai=self.ai,
        )
        return [posting] if posting else []


def announcement_url(page_url: str, added: list[str]) -> str:
    """The page's address, marked per announcement.

    Jobs are told apart by link, so a second drive announced on the same page, with no link of its own, would
    otherwise look like the first one and be dropped as already seen.
    """
    mark = hashlib.sha1("\n".join(added).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    parts = urlsplit(page_url)
    query = f"{parts.query}&sarthi={mark}" if parts.query else f"sarthi={mark}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
