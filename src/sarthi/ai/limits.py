"""Staying inside a cloud provider's rate limits, with what the user waits on going first.

Free tiers are small (Groq: 8,000 tokens a minute and 1,000 requests a day per model) and every Sarthi client
shares one key. Providers say what is left in their response headers, so Sarthi reads them and:

- waits for the minute's window to reset instead of sending a call that will be refused;
- lets background work (job scoring) wait while a call the user waits on (documents, chat) wants the model;
- stops background work when only a reserve of the day's requests is left, so the user's work never runs dry.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass, field

REQUEST_RESERVE = 150  # requests a day kept for what the user waits on
COMPLETION_ALLOWANCE = 700  # tokens an answer usually takes (reasoning models think before answering)
CHARS_PER_TOKEN = 4
YIELD_STEP_S = 2.0  # how often waiting background work looks again
_DURATION = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|h|m|s)")
_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}

Monotonic = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def parse_duration(text: str) -> float | None:
    """Seconds in a reset header (``52.6s``, ``24m28.8s``, ``1h2m``, ``120ms``); None if unreadable."""
    text = text.strip()
    parts = list(_DURATION.finditer(text))
    if not parts or "".join(p.group(0) for p in parts) != text:
        return None
    return sum(float(p["value"]) * _SECONDS[p["unit"]] for p in parts)


def estimate_tokens(prompt_chars: int) -> int:
    """A cautious guess of what one call costs: the prompt plus a usual answer."""
    return prompt_chars // CHARS_PER_TOKEN + COMPLETION_ALLOWANCE


def _int(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


@dataclass
class RateWindow:
    """What one model at one provider has left, as its last response said."""

    clock: Monotonic = time.monotonic
    sleep: Sleep = asyncio.sleep
    tokens_left: int | None = None  # None: unknown, so assume enough
    tokens_reset_at: float = 0.0
    requests_left: int | None = None
    requests_reset_at: float = 0.0
    user_calls: int = field(default=0)  # calls the user waits on, waiting or running

    def update(self, headers: Mapping[str, str]) -> None:
        """Take in a response's ``x-ratelimit-*`` headers (OpenAI and Groq style)."""
        now = self.clock()
        if (tokens := _int(headers, "x-ratelimit-remaining-tokens")) is not None:
            self.tokens_left = tokens
            self.tokens_reset_at = now + (parse_duration(headers.get("x-ratelimit-reset-tokens", "")) or 0)
        if (requests := _int(headers, "x-ratelimit-remaining-requests")) is not None:
            self.requests_left = requests
            self.requests_reset_at = now + (
                parse_duration(headers.get("x-ratelimit-reset-requests", "")) or 0
            )

    def _forget_expired(self, now: float) -> None:
        if self.tokens_left is not None and now >= self.tokens_reset_at:
            self.tokens_left = None
        if self.requests_left is not None and now >= self.requests_reset_at:
            self.requests_left = None

    def _token_wait(self, tokens: int, now: float) -> float:
        if self.tokens_left is None or self.tokens_left >= tokens:
            return 0.0
        return max(0.0, self.tokens_reset_at - now)

    async def admit(self, tokens: int, *, background: bool, max_wait: float) -> bool:
        """Wait (at most ``max_wait`` seconds) until a call of ``tokens`` fits; False means use another model.

        Background calls also wait while the user's calls want this model, and are refused outright once the
        day's requests are down to the reserve.
        """
        waited = 0.0
        while True:
            now = self.clock()
            self._forget_expired(now)
            if background and self.requests_left is not None and self.requests_left <= REQUEST_RESERVE:
                return False
            wait = self._token_wait(tokens, now)
            if background and self.user_calls:
                wait = max(wait, YIELD_STEP_S)
            if wait <= 0:
                break
            if waited + wait > max_wait:
                return False
            await self.sleep(wait)
            waited += wait
        if self.tokens_left is not None:
            self.tokens_left -= tokens  # held until the response says what is really left
        if self.requests_left is not None:
            self.requests_left -= 1
        return True

    @contextlib.contextmanager
    def user_call(self, *, active: bool) -> Generator[None]:
        """Mark a call the user waits on (``active``) for its whole wait and run."""
        if active:
            self.user_calls += 1
        try:
            yield
        finally:
            if active:
                self.user_calls -= 1


class RateLimits:
    """One window per model, shared by every client in the process (they share the provider's key)."""

    def __init__(self, clock: Monotonic = time.monotonic, sleep: Sleep = asyncio.sleep) -> None:
        """Windows created here use ``clock`` and ``sleep`` (fakes in tests)."""
        self._clock = clock
        self._sleep = sleep
        self._windows: dict[str, RateWindow] = {}

    def window(self, label: str) -> RateWindow:
        """The window for ``provider/model``."""
        if label not in self._windows:
            self._windows[label] = RateWindow(self._clock, self._sleep)
        return self._windows[label]


RATE_LIMITS = RateLimits()
