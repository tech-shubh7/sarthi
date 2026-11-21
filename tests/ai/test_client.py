import json
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from sarthi.ai.client import AiClient, AiUnavailableError, BudgetExhaustedError
from sarthi.ai.limits import RateLimits
from sarthi.ai.prompts import Prompt
from sarthi.ai.providers import Endpoint
from sarthi.db.ai_store import AiStore
from tests.ai.test_limits import FakeTime
from tests.conftest import FakeClock

PROMPT = Prompt("test", "1", "Say hi.")
GEMINI = Endpoint("gemini", "flash", "https://gemini.test/v1", SecretStr("secret-gemini-key"))
OPENAI = Endpoint("openai", "mini", "https://openai.test/v1", SecretStr("secret-openai-key"))
OLLAMA = Endpoint("ollama", "llama", "http://localhost:11434/v1", None)


class Answer(BaseModel):
    greeting: str
    count: int


Responder = Callable[[httpx.Request], httpx.Response]


def reply(content: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    return httpx.Response(200, json=body)


class FakeProviders:
    """Per-host scripted replies; records every request."""

    def __init__(self, script: dict[str, list[httpx.Response | Exception]]) -> None:
        self.script = script
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.script[request.url.host].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def store(conn: sqlite3.Connection, clock: FakeClock) -> AiStore:
    return AiStore(conn, clock=clock)


def client(
    store: AiStore, providers: FakeProviders, endpoints: list[Endpoint], clock: FakeClock, budget: int = 0
) -> AiClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(providers))
    return AiClient(
        endpoints, store, daily_token_budget=budget, requests_per_minute=6000, http=http, clock=clock
    )


async def test_valid_answer_is_returned_cached_and_counted(store: AiStore, clock: FakeClock) -> None:
    providers = FakeProviders({"gemini.test": [reply('```json\n{"greeting": "hi", "count": 2}\n```')]})
    ai = client(store, providers, [GEMINI], clock)
    first = await ai.structured(PROMPT, Answer, job_posting="Backend role")
    again = await ai.structured(PROMPT, Answer, job_posting="Backend role")
    assert first == again == Answer(greeting="hi", count=2)
    assert len(providers.requests) == 1  # second answer came from the cache
    request = providers.requests[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-gemini-key"
    body = providers.bodies()[0]
    assert (body["model"], body["temperature"], body["response_format"]) == (
        "flash",
        0,
        {"type": "json_object"},
    )
    assert body["messages"][0]["role"] == "system"
    assert "<job_posting>\nBackend role\n</job_posting>" in body["messages"][1]["content"]
    assert store.tokens_since("2026-01-01T00:00:00.000000Z") == 15
    await ai.aclose()


async def test_invalid_json_gets_one_repair(store: AiStore, clock: FakeClock) -> None:
    providers = FakeProviders(
        {"gemini.test": [reply('{"greeting": "hi"}'), reply('{"greeting": "hi", "count": 1}')]}
    )
    result = await client(store, providers, [GEMINI], clock).structured(PROMPT, Answer)
    assert result.count == 1
    repair = providers.bodies()[1]["messages"]
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert "count" in repair[-1]["content"]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429),
        httpx.Response(503),
        httpx.Response(401, json={"error": "bad key secret-gemini-key"}),
        httpx.ConnectError("refused"),
        reply("not json at all"),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_failures_fall_back_to_the_next_model(
    store: AiStore, clock: FakeClock, failure: httpx.Response | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    gemini_script: list[httpx.Response | Exception] = [failure]
    if isinstance(failure, httpx.Response) and failure.status_code == 200 and "choices" in failure.text:
        gemini_script.append(reply("still not json"))  # the repair attempt
    providers = FakeProviders(
        {"gemini.test": gemini_script, "openai.test": [reply('{"greeting": "ok", "count": 3}')]}
    )
    result = await client(store, providers, [GEMINI, OPENAI], clock).structured(PROMPT, Answer)
    assert result.greeting == "ok"
    assert "secret-gemini-key" not in caplog.text


async def test_all_models_failing_is_reported(store: AiStore, clock: FakeClock) -> None:
    providers = FakeProviders({"gemini.test": [httpx.Response(500)], "openai.test": [httpx.Response(500)]})
    with pytest.raises(AiUnavailableError):
        await client(store, providers, [GEMINI, OPENAI], clock).structured(PROMPT, Answer)
    with pytest.raises(AiUnavailableError, match="no AI model is configured"):
        await client(store, providers, [], clock).structured(PROMPT, Answer)


async def test_budget_skips_paid_models_but_not_local_ones(store: AiStore, clock: FakeClock) -> None:
    store.record_usage("gemini", "flash", 900, 100)  # today's budget used up
    providers = FakeProviders({"localhost": [reply('{"greeting": "local", "count": 0}')]})
    result = await client(store, providers, [GEMINI, OLLAMA], clock, budget=1000).structured(PROMPT, Answer)
    assert result.greeting == "local"
    assert [r.url.host for r in providers.requests] == ["localhost"]


async def test_budget_spent_with_only_paid_models(store: AiStore, clock: FakeClock) -> None:
    store.record_usage("gemini", "flash", 1000, 0)
    with pytest.raises(BudgetExhaustedError) as caught:
        await client(store, FakeProviders({}), [GEMINI], clock, budget=1000).structured(PROMPT, Answer)
    assert caught.value.retry_after == pytest.approx(15 * 3600)  # clock is 09:00 UTC


async def test_budget_resets_the_next_day(store: AiStore, clock: FakeClock) -> None:
    store.record_usage("gemini", "flash", 1000, 0)
    clock.advance(16 * 3600)
    providers = FakeProviders({"gemini.test": [reply('{"greeting": "new day", "count": 1}')]})
    result = await client(store, providers, [GEMINI], clock, budget=1000).structured(PROMPT, Answer)
    assert result.greeting == "new day"


async def test_local_models_get_a_longer_timeout(store: AiStore, clock: FakeClock) -> None:
    timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return reply('{"greeting": "hi", "count": 1}')

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    local = AiClient([OLLAMA], store, requests_per_minute=6000, http=http, clock=clock)
    cloud = AiClient([GEMINI], store, requests_per_minute=6000, http=http, clock=clock)
    await local.structured(PROMPT, Answer)
    await cloud.structured(PROMPT, Answer, job_posting="other input")
    assert timeouts == [600.0, 90.0]


async def test_a_short_rate_limit_is_waited_out_once(store: AiStore, clock: FakeClock) -> None:
    slow_down = httpx.Response(429, headers={"retry-after": "0"})
    providers = FakeProviders({"gemini.test": [slow_down, reply('{"greeting": "after wait", "count": 1}')]})
    result = await client(store, providers, [GEMINI, OLLAMA], clock).structured(PROMPT, Answer)
    assert result.greeting == "after wait"
    assert [r.url.host for r in providers.requests] == ["gemini.test", "gemini.test"]


async def test_a_long_rate_limit_moves_on_to_the_next_model(store: AiStore, clock: FakeClock) -> None:
    long_wait = httpx.Response(429, headers={"retry-after": "3600"})
    providers = FakeProviders(
        {"gemini.test": [long_wait], "localhost": [reply('{"greeting": "local", "count": 1}')]}
    )
    result = await client(store, providers, [GEMINI, OLLAMA], clock).structured(PROMPT, Answer)
    assert result.greeting == "local"


@pytest.mark.parametrize(("urgent", "waited"), [(False, True), (True, False)])
async def test_background_work_waits_longer_for_a_fast_model(
    store: AiStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, urgent: bool, waited: bool
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("sarthi.ai.client.asyncio.sleep", fake_sleep)
    half_minute = httpx.Response(429, headers={"retry-after": "30"})
    providers = FakeProviders(
        {
            "gemini.test": [half_minute, reply('{"greeting": "fast", "count": 1}')],
            "localhost": [reply('{"greeting": "local", "count": 1}')],
        }
    )
    result = await client(store, providers, [GEMINI, OLLAMA], clock).structured(PROMPT, Answer, urgent=urgent)
    assert result.greeting == ("fast" if waited else "local")
    assert (30.0 in sleeps) is waited


def limited_client(
    store: AiStore,
    providers: FakeProviders,
    endpoints: list[Endpoint],
    clock: FakeClock,
    limits: RateLimits,
    *,
    background: bool = False,
) -> AiClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(providers))
    return AiClient(
        endpoints,
        store,
        requests_per_minute=6000,
        http=http,
        clock=clock,
        background=background,
        limits=limits,
    )


def with_limits(response: httpx.Response, tokens: int, requests: int = 900) -> httpx.Response:
    response.headers.update(
        {
            "x-ratelimit-remaining-tokens": str(tokens),
            "x-ratelimit-reset-tokens": "40s",
            "x-ratelimit-remaining-requests": str(requests),
            "x-ratelimit-reset-requests": "20m",
        }
    )
    return response


async def test_a_refused_call_says_why(
    store: AiStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    refused = httpx.Response(
        400, json={"error": {"message": "Failed to generate JSON. Please adjust your prompt."}}
    )
    providers = FakeProviders({"gemini.test": [refused]})
    with pytest.raises(AiUnavailableError):
        await client(store, providers, [GEMINI], clock).structured(PROMPT, Answer)
    assert "HTTP 400 (rejected): Failed to generate JSON" in caplog.text


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": {"message": "key secret-gemini-key has no access to this model"}}),
        (401, {"error": {"message": "Invalid API Key"}}),
        (403, {"error": "forbidden for secret-gemini-key"}),
        (429, {"error": ["not", "a", "dict"]}),
        (500, ["odd", "body"]),
    ],
)
async def test_provider_messages_never_log_the_key(
    store: AiStore, clock: FakeClock, caplog: pytest.LogCaptureFixture, status: int, body: Any
) -> None:
    providers = FakeProviders({"gemini.test": [httpx.Response(status, json=body)]})
    with pytest.raises(AiUnavailableError):
        await client(store, providers, [GEMINI], clock).structured(PROMPT, Answer)
    assert f"HTTP {status}" in caplog.text
    assert "secret-gemini-key" not in caplog.text


async def test_a_full_minute_is_waited_out_instead_of_refused(store: AiStore, clock: FakeClock) -> None:
    time = FakeTime()
    limits = RateLimits(time, time.sleep)
    providers = FakeProviders(
        {
            "gemini.test": [
                with_limits(reply('{"greeting": "one", "count": 1}'), tokens=100),
                reply('{"greeting": "two", "count": 2}'),
            ]
        }
    )
    ai = limited_client(store, providers, [GEMINI], clock, limits)
    assert (await ai.structured(PROMPT, Answer, job_posting="a")).greeting == "one"
    assert (await ai.structured(PROMPT, Answer, job_posting="b")).greeting == "two"
    assert time.slept == [pytest.approx(40.0)]  # waited for the window instead of being refused
    assert len(providers.requests) == 2


async def test_background_work_leaves_the_last_requests_to_the_user(store: AiStore, clock: FakeClock) -> None:
    time = FakeTime()
    limits = RateLimits(time, time.sleep)
    providers = FakeProviders(
        {
            "gemini.test": [
                with_limits(reply('{"greeting": "user", "count": 1}'), tokens=7000, requests=100),
                reply('{"greeting": "user again", "count": 2}'),
            ]
        }
    )
    user = limited_client(store, providers, [GEMINI], clock, limits)
    scoring = limited_client(store, providers, [GEMINI], clock, limits, background=True)
    assert (await user.structured(PROMPT, Answer, job_posting="a")).greeting == "user"
    with pytest.raises(AiUnavailableError):
        await scoring.structured(PROMPT, Answer, job_posting="b")  # only the reserve is left
    assert (await user.structured(PROMPT, Answer, job_posting="c")).greeting == "user again"
    assert len(providers.requests) == 2  # the background call never reached the provider
