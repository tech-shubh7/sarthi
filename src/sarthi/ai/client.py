# Structured-call flow adapted from JobRadar (MIT), src/jobradar/llm.py (LLM.structured) @ fdd739b, and the
# model fallback chain from ApplyPilot (AGPL-3.0), src/applypilot/llm.py @ ibarrajo fork.
# Copyright (c) ThePlator; ApplyPilot contributors. Modifications (c) Sarthi contributors.
# Changes: plain httpx against OpenAI-compatible endpoints instead of LiteLLM; user-ordered model chain;
# token budget instead of a price table; local providers exempt from the budget.
"""One way to ask an AI model for a validated JSON answer: cached, budgeted, paced, with fallbacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from sarthi.ai.gate import local_gate
from sarthi.ai.limits import RATE_LIMITS, RateLimits, estimate_tokens
from sarthi.ai.prompts import SYSTEM_RULES, Prompt
from sarthi.ai.providers import FREE_PROVIDERS, Endpoint
from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.ai_store import AiStore

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

REQUEST_TIMEOUT_S = 90.0
LOCAL_TIMEOUT_S = 600.0  # a local model on a laptop CPU can take minutes for one answer
MAX_ERROR_CHARS = 800
MAX_PROVIDER_MESSAGE = 200
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_HTTP_OK = 200
_SERVER_ERROR = 500
_RETRY_STATUSES = frozenset({408, 409, 429})
_LOGIN_FAILED = frozenset({401, 403})


class AiError(Exception):
    """Base for AI-layer failures callers may want to handle."""


class BudgetExhaustedError(AiError):
    """The daily token budget is spent; ``retry_after`` seconds until it resets."""

    def __init__(self, retry_after: float) -> None:
        """Remember when the budget resets."""
        super().__init__(f"daily AI token budget reached; resets in {retry_after / 3600:.1f} h")
        self.retry_after = retry_after


class AiUnavailableError(AiError):
    """No configured model gave a valid answer."""


def _parse_json(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT.search(content)  # tolerate ```json fences or a stray sentence
        if match:
            return json.loads(match.group(0))
        raise


_TOO_MANY_REQUESTS = 429
MAX_RETRY_WAIT_S = 10.0  # the user is waiting on a reply: fall back to the next model quickly
MAX_BACKGROUND_WAIT_S = (
    60.0  # background work (scoring, documents): a fast model after a minute beats a slow one
)


def _retry_after(response: httpx.Response, limit: float = MAX_RETRY_WAIT_S) -> float | None:
    """Seconds a rate-limited provider asks us to wait, when short enough to wait for; else None."""
    raw = response.headers.get("retry-after", "")
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= limit else None


def _provider_message(response: httpx.Response, endpoint: Endpoint) -> str:
    """The provider's short explanation of a refused call (e.g. which limit was hit), safe to log.

    Login failures say nothing (their text may quote the key); the key is removed from any other message.
    """
    if response.status_code in _LOGIN_FAILED:
        return ""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = str((error.get("message") if isinstance(error, dict) else error) or "")
    if endpoint.api_key is not None and (key := endpoint.api_key.get_secret_value()):
        message = message.replace(key, "[key]")
    return message[:MAX_PROVIDER_MESSAGE]


class _CallFailedError(Exception):
    """One endpoint failed; try the next."""


class AiClient:
    """Asks models for JSON validated against a pydantic schema."""

    def __init__(  # noqa: PLR0913  (explicit, injectable dependencies)
        self,
        endpoints: Sequence[Endpoint],
        store: AiStore,
        *,
        daily_token_budget: int = 0,
        requests_per_minute: int = 30,
        http: httpx.AsyncClient | None = None,
        clock: Clock = utcnow,
        background: bool = False,
        limits: RateLimits = RATE_LIMITS,
    ) -> None:
        """Try ``endpoints`` in order. ``daily_token_budget`` 0 means no limit.

        A ``background`` client (job scoring) gives way to the user's calls within a provider's rate limits.
        """
        self.endpoints = tuple(endpoints)
        self._background = background
        self._limits = limits
        self._store = store
        self._budget = daily_token_budget
        self._interval = 60.0 / requests_per_minute
        self._http = http or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._next_call = 0.0

    @property
    def available(self) -> bool:
        """True when at least one model is configured."""
        return bool(self.endpoints)

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._http.aclose()

    async def structured(self, prompt: Prompt, schema: type[T], *, urgent: bool = False, **data: str) -> T:
        """Run ``prompt`` with ``data`` and return the validated answer (cached by model, version and input).

        Raises ``BudgetExhaustedError`` when only paid models are left and the budget is spent, and
        ``AiUnavailableError`` when no model answers validly.
        """
        if not self.endpoints:
            raise AiUnavailableError("no AI model is configured")
        text = prompt.render(**data)
        budget_hit: BudgetExhaustedError | None = None
        for endpoint in self.endpoints:
            key = hashlib.sha256(
                f"{endpoint.label}|{prompt.name}|{prompt.version}|{text}".encode()
            ).hexdigest()
            if (cached := self._store.cache_get(key)) is not None:
                try:
                    return schema.model_validate_json(cached)
                except ValidationError:
                    pass  # schema changed since it was cached: ask again
            if not endpoint.free:
                try:
                    self._check_budget()
                except BudgetExhaustedError as error:
                    budget_hit = error
                    continue
            try:
                if endpoint.free:  # a local model: one request at a time, the user's chat first
                    async with local_gate(endpoint.base_url).hold(urgent=urgent):
                        result = await self._ask(endpoint, text, schema, urgent=urgent)
                else:
                    window = self._limits.window(endpoint.label)
                    with window.user_call(active=not self._background):
                        admitted = await window.admit(
                            estimate_tokens(len(SYSTEM_RULES) + len(text)),
                            background=self._background,
                            max_wait=MAX_RETRY_WAIT_S if urgent else MAX_BACKGROUND_WAIT_S,
                        )
                        if not admitted:
                            log.info("AI model %s skipped: its rate limit is used up for now", endpoint.label)
                            continue
                        result = await self._ask(endpoint, text, schema, urgent=urgent)
            except _CallFailedError as error:
                log.warning("AI model %s failed: %s", endpoint.label, error)
                continue
            self._store.cache_put(key, result.model_dump_json())
            return result
        if budget_hit is not None:
            raise budget_hit
        raise AiUnavailableError("no configured AI model gave a valid answer")

    def _check_budget(self) -> None:
        if self._budget <= 0:
            return
        now = self._clock()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used = self._store.tokens_since(to_iso(midnight), exclude_providers=FREE_PROVIDERS)
        if used >= self._budget:
            raise BudgetExhaustedError(max(60.0, (midnight + timedelta(days=1) - now).total_seconds()))

    async def _ask(self, endpoint: Endpoint, text: str, schema: type[T], *, urgent: bool = False) -> T:
        messages = [{"role": "system", "content": SYSTEM_RULES}, {"role": "user", "content": text}]
        content = await self._call(endpoint, messages, urgent=urgent)
        try:
            return schema.model_validate(_parse_json(content))
        except (json.JSONDecodeError, ValidationError) as first_error:
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "That did not match the requested JSON shape: "
                    f"{str(first_error)[:MAX_ERROR_CHARS]}\nReturn only the corrected JSON object.",
                },
            ]
        content = await self._call(endpoint, messages, urgent=urgent)
        try:
            return schema.model_validate(_parse_json(content))
        except (json.JSONDecodeError, ValidationError) as error:
            raise _CallFailedError(f"invalid JSON after one repair: {str(error)[:200]}") from error

    async def _pace(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next_call > now:
                await asyncio.sleep(self._next_call - now)
            self._next_call = max(now, self._next_call) + self._interval

    async def _post(
        self, endpoint: Endpoint, body: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        try:
            response = await self._http.post(
                f"{endpoint.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=LOCAL_TIMEOUT_S if endpoint.free else REQUEST_TIMEOUT_S,
            )
        except httpx.HTTPError as error:
            raise _CallFailedError(type(error).__name__) from error
        if not endpoint.free:
            self._limits.window(endpoint.label).update(response.headers)
        return response

    async def _call(self, endpoint: Endpoint, messages: list[dict[str, str]], *, urgent: bool = False) -> str:
        await self._pace()
        headers = (
            {"Authorization": f"Bearer {endpoint.api_key.get_secret_value()}"} if endpoint.api_key else {}
        )
        body = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        response = await self._post(endpoint, body, headers)
        limit = MAX_RETRY_WAIT_S if urgent else MAX_BACKGROUND_WAIT_S
        if response.status_code == _TOO_MANY_REQUESTS and (wait := _retry_after(response, limit)) is not None:
            await asyncio.sleep(wait)  # a "slow down" from a fast provider: worth one wait
            response = await self._post(endpoint, body, headers)
        if response.status_code != _HTTP_OK:
            kind = (
                "temporary"
                if response.status_code >= _SERVER_ERROR or response.status_code in _RETRY_STATUSES
                else "rejected"
            )
            detail = _provider_message(response, endpoint)
            raise _CallFailedError(
                f"HTTP {response.status_code} ({kind})" + (f": {detail}" if detail else "")
            )
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise _CallFailedError("unexpected response shape") from error
        usage = payload.get("usage") or {}
        self._store.record_usage(
            endpoint.provider,
            endpoint.model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
        return str(content)
