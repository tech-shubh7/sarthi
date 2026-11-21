"""Telegram job channels, read with the user's own Telegram account (read-only).

Idea from JobRadar's Telethon reader (MIT).

Sarthi never sends, joins, or marks anything read: it reads recent posts of public channels (or channels the
user already joined) after the user logs in once with ``sarthi login telegram``. Each post is read like any
post (labelled lines, then the AI). The session file grants account access, so it is kept owner-only.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from sarthi.ai.client import AiClient
from sarthi.clock import Clock, utcnow
from sarthi.jobs.models import JobPosting
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources.base import SearchQuery
from sarthi.sources.posts import post_to_job

log = logging.getLogger(__name__)

MAX_PER_CHANNEL = 200
SESSION_FILE = "telegram"  # Telethon adds ".session"


def protect(session: Path) -> None:
    """The session file grants full account access: owner read/write only."""
    path = session.with_suffix(".session")
    if path.exists():
        path.chmod(0o600)


def message_text(message: Any) -> str:
    """The post's text plus the links its buttons and link entities carry."""
    from telethon.tl.types import MessageEntityTextUrl  # noqa: PLC0415  (optional dependency, loaded late)

    text = str(getattr(message, "message", "") or "")
    urls = [e.url for e in getattr(message, "entities", None) or [] if isinstance(e, MessageEntityTextUrl)]
    for row in getattr(getattr(message, "reply_markup", None), "rows", None) or []:
        for button in getattr(row, "buttons", []) or []:
            if url := getattr(button, "url", None):
                urls.append(url)
    return "\n".join([text, *dict.fromkeys(urls)])


class ChannelSource:
    """Recent job posts in the user's Telegram channels."""

    name = "feed:telegram"

    def __init__(  # noqa: PLR0913  (account details)
        self,
        channels: list[str],
        api_id: int,
        api_hash: str,
        session: Path,
        *,
        ai: AiClient | None = None,
        clock: Clock = utcnow,
    ) -> None:
        """Read ``channels`` (usernames like ``@jobs_channel`` or invite-free public links)."""
        self.channels = [c.strip().removeprefix("https://t.me/").lstrip("@") for c in channels if c.strip()]
        self.api_id, self.api_hash, self.session = api_id, api_hash, session
        self.ai = ai
        self._clock = clock

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Job posts newer than ``query.hours_old`` (24 hours when unset)."""
        from telethon import TelegramClient  # noqa: PLC0415
        from telethon.errors import FloodWaitError, RPCError  # noqa: PLC0415

        if not self.session.with_suffix(".session").exists():
            raise PermanentError(
                "Telegram channels need you to log in once: run `uv run sarthi login telegram`"
            )
        since = self._clock() - timedelta(hours=query.hours_old or 24)
        client = TelegramClient(str(self.session), self.api_id, self.api_hash)
        postings: list[JobPosting] = []
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise PermanentError("the Telegram login expired: run `uv run sarthi login telegram` again")
            for channel in self.channels:
                try:
                    async for message in client.iter_messages(channel, limit=MAX_PER_CHANNEL):
                        if message.date is None or message.date < since:
                            break
                        posting = await post_to_job(
                            message_text(message),
                            source="telegram",
                            posted_at=message.date,
                            ai=self.ai,
                            post_id=f"{channel}/{message.id}",
                        )
                        if posting is not None:
                            postings.append(posting)
                except FloodWaitError as error:
                    raise RetryableError(
                        "Telegram asked to slow down", retry_after=float(error.seconds)
                    ) from None
                except (RPCError, ValueError) as error:  # a channel that does not exist or is private
                    log.warning("telegram channel %s skipped: %s", channel, type(error).__name__)
        except OSError as error:
            raise RetryableError(f"telegram: {type(error).__name__}") from None
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
            protect(self.session)
        return postings


async def login(api_id: int, api_hash: str, session: Path) -> str:
    """Log in in the terminal (phone number, then the code Telegram sends). Returns the account's name."""
    from telethon import TelegramClient  # noqa: PLC0415

    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), api_id, api_hash)
    try:
        await client.start()  # asks for the phone number and code in the terminal
        me = await client.get_me()
        return str(getattr(me, "first_name", "") or "your account")
    finally:
        await client.disconnect()
        protect(session)
