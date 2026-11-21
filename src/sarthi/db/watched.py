"""Storage for what the user asked Sarthi to watch (career links, hiring pages) and each page's last text."""

from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction

MAX_WATCHED = 500
MAX_TEXT = 500


@dataclass(frozen=True, slots=True)
class Watched:
    """One watched link: a company board on a known hiring system, or a page compared for new lines."""

    url: str
    kind: str  # "board" or "page"
    system: str = ""
    slug: str = ""
    name: str = ""


class WatchStore:
    """SQL for the ``watched`` and ``page_text`` tables."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn``; ``clock`` stamps changes."""
        self._conn = conn
        self._clock = clock

    def add(self, item: Watched) -> bool:
        """Start watching. False when it was watched already or the list is full."""
        if self.count() >= MAX_WATCHED:
            return False
        with transaction(self._conn) as c:
            cursor = c.execute(
                "INSERT OR IGNORE INTO watched (url, kind, system, slug, name, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.url[:MAX_TEXT],
                    item.kind,
                    item.system,
                    item.slug[:MAX_TEXT],  # Workday and iCIMS slugs are whole links
                    item.name[:200],
                    to_iso(self._clock()),
                ),
            )
            return cursor.rowcount > 0

    def remove(self, url: str) -> bool:
        """Stop watching. True when it was watched."""
        with transaction(self._conn) as c:
            c.execute("DELETE FROM page_text WHERE url = ?", (url,))
            return c.execute("DELETE FROM watched WHERE url = ?", (url,)).rowcount > 0

    def all(self) -> list[Watched]:
        """Everything watched, oldest first."""
        rows = self._conn.execute("SELECT url, kind, system, slug, name FROM watched ORDER BY added_at")
        return [Watched(r["url"], r["kind"], r["system"], r["slug"], r["name"]) for r in rows]

    def count(self) -> int:
        """How many links are watched."""
        return int(self._conn.execute("SELECT count(*) FROM watched").fetchone()[0])

    def page_text(self, url: str) -> str | None:
        """The page's text at the last read, or None before the first."""
        row = self._conn.execute("SELECT text_zlib FROM page_text WHERE url = ?", (url,)).fetchone()
        return zlib.decompress(row["text_zlib"]).decode("utf-8") if row else None

    def set_page_text(self, url: str, text: str) -> None:
        """Remember the page's text for the next comparison."""
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO page_text (url, text_zlib, read_at) VALUES (?, ?, ?) ON CONFLICT (url)"
                " DO UPDATE SET text_zlib = excluded.text_zlib, read_at = excluded.read_at",
                (url, zlib.compress(text.encode("utf-8")), to_iso(self._clock())),
            )
