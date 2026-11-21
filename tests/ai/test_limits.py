import asyncio

import pytest

from sarthi.ai.limits import (
    REQUEST_RESERVE,
    RateLimits,
    RateWindow,
    estimate_tokens,
    parse_duration,
    retry_seconds,
)


class FakeTime:
    """A monotonic clock that only moves when someone sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def window(time: FakeTime) -> RateWindow:
    return RateWindow(time, time.sleep)


GROQ_HEADERS = {
    "x-ratelimit-remaining-tokens": "978",
    "x-ratelimit-reset-tokens": "52.5s",
    "x-ratelimit-remaining-requests": "983",
    "x-ratelimit-reset-requests": "24m28.8s",
}


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("52.665s", 52.665),
        ("24m28.8s", 1468.8),
        ("1h2m", 3720.0),
        ("120ms", 0.12),
        ("7s", 7.0),
        ("", None),
        ("soon", None),
        ("5 s", None),
        ("12", None),
        ("-3s", None),
        ("1m garbage", None),
    ],
)
def test_reset_durations(text: str, seconds: float | None) -> None:
    result = parse_duration(text)
    assert result == pytest.approx(seconds) if seconds is not None else result is None


@pytest.mark.parametrize(
    ("headers", "message", "seconds"),
    [
        ({"retry-after": "30"}, "", 30.0),
        ({"retry-after": "30"}, "Please try again in 9m14.5s.", 30.0),  # the header wins
        ({}, "Used 199616, Requested 1714. Please try again in 9m14.592s. Need more tokens?", 554.592),
        ({"retry-after": "soon"}, "please TRY AGAIN IN 2h", 7200.0),
        ({"retry-after": "-5"}, "", None),
        ({}, "Rate limit reached.", None),
        ({}, "try again in a while", None),
    ],
)
def test_how_long_a_refusal_says_to_wait(
    headers: dict[str, str], message: str, seconds: float | None
) -> None:
    result = retry_seconds(headers, message)
    assert result == pytest.approx(seconds) if seconds is not None else result is None


async def test_after_a_refusal_the_model_is_left_alone_until_it_resets() -> None:
    time = FakeTime()
    w = window(time)
    w.refused(600)
    assert not await w.admit(10, background=False, max_wait=60)  # no call: the provider said wait
    assert time.slept == []
    assert await w.admit(10, background=True, max_wait=601)  # a patient caller waits it out
    assert time.slept == [pytest.approx(600)]
    w.refused(5)
    w.refused(1)  # a shorter wait never cuts a longer one short
    assert w.blocked_until == pytest.approx(time.now + 5)


def test_estimate_counts_the_prompt_and_an_answer() -> None:
    assert estimate_tokens(4000) > 1000
    assert estimate_tokens(0) > 0


def test_headers_are_read_and_bad_ones_ignored() -> None:
    time = FakeTime()
    w = window(time)
    w.update(GROQ_HEADERS)
    assert (w.tokens_left, w.requests_left) == (978, 983)
    assert w.tokens_reset_at == pytest.approx(time.now + 52.5)
    w.update({"x-ratelimit-remaining-tokens": "lots"})  # unreadable: keep what we knew
    assert w.tokens_left == 978
    w.update({})
    assert w.requests_left == 983


async def test_unknown_limits_let_calls_through_at_once() -> None:
    time = FakeTime()
    assert await window(time).admit(5000, background=True, max_wait=0)
    assert time.slept == []


async def test_a_call_that_does_not_fit_waits_for_the_reset() -> None:
    time = FakeTime()
    w = window(time)
    w.update(GROQ_HEADERS)  # 978 tokens left, reset in 52.5 s
    assert await w.admit(2000, background=False, max_wait=60)
    assert time.slept == [pytest.approx(52.5)]
    assert w.tokens_left is None  # the window reset: unknown again until the next response


async def test_a_reset_too_far_away_means_another_model() -> None:
    time = FakeTime()
    w = window(time)
    w.update(GROQ_HEADERS)
    assert not await w.admit(2000, background=False, max_wait=10)
    assert time.slept == []


async def test_admitted_calls_hold_their_tokens_until_the_next_response() -> None:
    time = FakeTime()
    w = window(time)
    w.update({"x-ratelimit-remaining-tokens": "3000", "x-ratelimit-reset-tokens": "30s"})
    assert await w.admit(2000, background=False, max_wait=0)
    assert not await w.admit(2000, background=False, max_wait=0)  # only 1000 left before the reset


async def test_background_keeps_the_days_last_requests_for_the_user() -> None:
    time = FakeTime()
    w = window(time)
    w.update({**GROQ_HEADERS, "x-ratelimit-remaining-requests": str(REQUEST_RESERVE)})
    assert not await w.admit(10, background=True, max_wait=600)
    assert await w.admit(10, background=False, max_wait=0)  # the user's own work still runs
    time.now += 24 * 60 + 30  # the day's request window reset
    assert await w.admit(10, background=True, max_wait=0)


async def test_background_waits_while_the_user_needs_the_model() -> None:
    time = FakeTime()
    w = window(time)
    with w.user_call(active=True):
        assert not await w.admit(10, background=True, max_wait=5)  # gives way, then gives up
        assert await w.admit(10, background=False, max_wait=0)  # the user's call is not held back
    assert w.user_calls == 0
    assert await w.admit(10, background=True, max_wait=0)


async def test_background_continues_once_the_user_call_ends() -> None:
    time = FakeTime()
    w = window(time)
    done = asyncio.Event()

    async def user() -> None:
        with w.user_call(active=True):
            await done.wait()

    task = asyncio.create_task(user())
    await asyncio.sleep(0)

    async def finish_soon() -> None:
        while not time.slept:
            await asyncio.sleep(0)
        done.set()

    finisher = asyncio.create_task(finish_soon())
    assert await w.admit(10, background=True, max_wait=60)
    await asyncio.gather(task, finisher)
    assert time.slept  # it waited for its turn


def test_user_call_counts_even_when_the_call_fails() -> None:
    w = window(FakeTime())
    with pytest.raises(RuntimeError), w.user_call(active=True):
        raise RuntimeError
    assert w.user_calls == 0
    with w.user_call(active=False):
        assert w.user_calls == 0


def test_one_window_per_model() -> None:
    time = FakeTime()
    limits = RateLimits(time, time.sleep)
    assert limits.window("groq/a") is limits.window("groq/a")
    assert limits.window("groq/a") is not limits.window("groq/b")
