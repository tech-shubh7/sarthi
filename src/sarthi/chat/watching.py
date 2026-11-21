"""/watch, /unwatch and /watching: careers links and hiring pages the user wants Sarthi to keep an eye on.

A link on a hiring system Sarthi knows (Greenhouse, Lever, Workday, ...) becomes that company's job board,
read like any other. Anything else is watched as a page: each read is compared with the last, and new
announcements become jobs. The link is checked here for shape only; whether it points to a public address
is checked every time the page is fetched, so a name that later moves to a private address is refused too.
"""

from __future__ import annotations

import ipaddress
from html import escape
from urllib.parse import urlsplit, urlunsplit

from ats_scrapers import resolve_careers_url
from ats_scrapers.scrapers import ScraperRegistry

from sarthi.chat.telegram.api import TelegramApi
from sarthi.db.watched import MAX_TEXT, MAX_WATCHED, Watched, WatchStore

MAX_LINK = MAX_TEXT  # what the database keeps whole
LIST_LIMIT = 30
EXAMPLE = "/watch https://jobs.lever.co/company"
USAGE = (
    "Send a careers link after the command, e.g.\n"
    "<code>/watch https://jobs.lever.co/company</code>\n"
    "<code>/watch https://www.tcs.com/careers/india/fresher-hiring</code>"
)
_LOCAL_NAMES = ("localhost", "localhost.localdomain")


def _link(text: str) -> str:
    return text.strip().strip("<>").strip()


class _NotWatchableError(ValueError):
    """The link cannot be watched; the message says why, for the user."""


def _web_link(link: str) -> tuple[str, str]:
    """``(normalised url, host)`` for a public-looking web link; raises ``_NotWatchableError`` otherwise."""
    if len(link) > MAX_LINK or any(ch.isspace() for ch in link):
        raise _NotWatchableError("That doesn't look like one web link. Send just the link, e.g. " + EXAMPLE)
    url = link if "://" in link else f"https://{link}"
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise _NotWatchableError("That doesn't look like a web link.") from None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or "." not in host.strip("."):
        raise _NotWatchableError("Only web links (http or https) can be watched.")
    if parts.username or parts.password:
        raise _NotWatchableError("Please send the link without a login inside it.")
    if host in _LOCAL_NAMES or host.endswith(".local") or _private_ip(host):
        raise _NotWatchableError(
            "That link points to this computer or a home network, not a public careers page."
        )
    url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    if len(url) > MAX_LINK:
        raise _NotWatchableError("That link is too long to watch.")
    return url, host


def watched_for(text: str) -> Watched | str:
    """What to watch for a link the user sent, or why it cannot be watched (a message for them)."""
    link = _link(text)
    if not link:
        return USAGE
    try:
        url, host = _web_link(link)
    except _NotWatchableError as problem:
        return str(problem)
    resolved = resolve_careers_url(url)
    if resolved is not None and str(resolved.ats) in _supported():
        system = str(resolved.ats)
        return Watched(url, "board", system=system, slug=resolved.slug, name=f"{host} ({system})")
    return Watched(url, "page", name=host)


def _private_ip(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        return False  # a name, not an address: checked when it is fetched


def _supported() -> set[str]:
    return {str(ats) for ats in ScraperRegistry.all()}


class WatchCommands:
    """Replies to /watch, /unwatch and /watching."""

    def __init__(self, api: TelegramApi, store: WatchStore) -> None:
        """Answer with ``api``; keep the list in ``store``."""
        self.api = api
        self.store = store

    async def watch(self, chat_id: int, argument: str) -> None:
        """Start watching the link in ``argument``."""
        item = watched_for(argument)
        if isinstance(item, str):
            await self.api.send_message(chat_id, item if item == USAGE else escape(item))
            return
        if self.store.count() >= MAX_WATCHED:
            await self.api.send_message(chat_id, f"You are watching {MAX_WATCHED} links, the most I keep.")
            return
        if not self.store.add(item):
            await self.api.send_message(chat_id, "👀 Already watching that one. /watching lists them all.")
            return
        what = (
            f"its job board on <b>{escape(item.system)}</b>: every new opening is matched for you"
            if item.kind == "board"
            else "the page: when something new is announced there, I'll read it and match it for you"
        )
        await self.api.send_message(
            chat_id, f"👀 Watching {escape(item.name)}: {what}.\nStop with /unwatch {escape(item.url)}"
        )

    async def unwatch(self, chat_id: int, argument: str) -> None:
        """Stop watching a link, given as the link itself or its number in /watching."""
        target = _link(argument)
        if not target:
            await self.api.send_message(
                chat_id, "Send the link or its number from /watching, e.g. /unwatch 2"
            )
            return
        items = self.store.all()
        url: str | None = None
        if target.isdigit():
            index = int(target) - 1
            url = items[index].url if 0 <= index < len(items) else None
        else:
            item = watched_for(target)
            url = item.url if isinstance(item, Watched) else None
        if url is None or not self.store.remove(url):
            await self.api.send_message(chat_id, "I'm not watching that. /watching lists what I watch.")
            return
        await self.api.send_message(chat_id, f"🛑 Stopped watching {escape(url)}")

    async def show(self, chat_id: int) -> None:
        """List what is watched, numbered for /unwatch."""
        items = self.store.all()
        if not items:
            await self.api.send_message(chat_id, "Nothing watched yet.\n\n" + USAGE)
            return
        lines = [f"<b>Watching {len(items)} link{'s' if len(items) != 1 else ''}</b>"]
        for number, item in enumerate(items[:LIST_LIMIT], start=1):
            kind = escape(item.system) if item.kind == "board" else "page"
            lines.append(f"{number}. {escape(item.url)} ({kind})")
        if len(items) > LIST_LIMIT:
            lines.append(f"…and {len(items) - LIST_LIMIT} more")
        lines.append("\nStop one with /unwatch and its number.")
        await self.api.send_message(chat_id, "\n".join(lines))
