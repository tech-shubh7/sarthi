from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from sarthi.app import ALIVE_KEY, SetupError, build, next_digest_at
from sarthi.cli import app as cli
from sarthi.clock import to_iso
from sarthi.config import Settings, get_settings
from sarthi.db.tasks import TaskStatus
from sarthi.profile.store import profile_path, save_profile
from tests.conftest import FakeClock
from tests.matching.conftest import make_job, make_profile

IST = timezone(timedelta(hours=5, minutes=30))


def test_next_digest_today_or_tomorrow() -> None:
    morning = datetime(2026, 11, 20, 9, 0, tzinfo=IST)
    evening = datetime(2026, 11, 20, 20, 0, tzinfo=IST)
    assert next_digest_at(morning, 19).astimezone(IST).hour == 19
    assert next_digest_at(morning, 19).astimezone(IST).date() == morning.date()
    assert next_digest_at(evening, 19).astimezone(IST).date() == morning.date() + timedelta(days=1)


def test_build_needs_a_bot_token(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="SARTHI_TELEGRAM_BOT_TOKEN"):
        build(Settings(_env_file=None, data_dir=tmp_path))


async def test_build_wires_the_worker_and_schedules_one_digest(tmp_path: Path, clock: FakeClock) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        telegram_bot_token=SecretStr("1:x"),
        llm_fast_models=[],
        llm_quality_models=[],
        watch_companies=["greenhouse/razorpay", "not-valid"],
    )
    sarthi = build(settings, clock=clock)
    try:
        assert set(sarthi.worker().handlers) | set(sarthi.user_worker().handlers) == {
            "source_check",
            "match",
            "alert",
            "digest",
            "catchup",
            "catchup_report",
            "documents",
            "documents_ready",
            "form_check",
            "ask_questions",
            "apply",
            "submit",
            "apply_preview",
            "apply_result",
            "mail_check",
            "quiet_check",
            "reply_notice",
            "followup_notice",
        }
        assert sarthi.bot.setup is not None
        assert set(sarthi.worker().handlers) == {
            "source_check",
            "match",
            "catchup",
            "mail_check",
            "quiet_check",
        }  # never blocks the user
        planned = set(sarthi.sources.planned)
        assert {
            "boards:linkedin+indeed",
            "greenhouse:razorpay",
            "search:freehire",
            "feed:hackernews",
        } <= planned
        assert len([name for name in planned if name.startswith("feed:")]) == len(settings.feeds)
        sarthi.schedule_digest()
        sarthi.schedule_digest()
        assert sarthi.queue.store.task_counts()[TaskStatus.PENDING] == 1
        assert not any(client.available for client in sarthi.ai_clients)

        sarthi.bot.jobs.save(make_job())
        assert sarthi.rematch_after_upgrade() == 1  # matching logic changed: recent jobs matched again
        assert sarthi.rematch_after_upgrade() == 0  # once
        assert sarthi.queue.active("match") == 1
    finally:
        await sarthi.bot.api.aclose()
        for client in sarthi.ai_clients:
            await client.aclose()
        sarthi.conn.close()


async def test_matching_never_uses_a_local_model(tmp_path: Path, clock: FakeClock) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        telegram_bot_token=SecretStr("1:x"),
        groq_api_key=SecretStr("k"),
        llm_fast_models=["groq/fast-model", "ollama/qwen2.5:3b"],
        llm_quality_models=[],
    )
    sarthi = build(settings, clock=clock)
    try:
        labels = [[e.label for e in client.endpoints] for client in sarthi.ai_clients]
        assert ["groq/fast-model", "ollama/qwen2.5:3b"] in labels  # reply tracking may still use it
        matching = sarthi.ai_clients[-1]
        assert [e.label for e in matching.endpoints] == ["groq/fast-model"]  # cloud models only
    finally:
        await sarthi.bot.api.aclose()
        for client in sarthi.ai_clients:
            await client.aclose()
        sarthi.conn.close()

    only_local = build(settings.model_copy(update={"llm_fast_models": ["ollama/qwen2.5:3b"]}), clock=clock)
    try:
        assert not only_local.ai_clients[-1].available  # nothing to call: matching uses the free score
    finally:
        await only_local.bot.api.aclose()
        for client in only_local.ai_clients:
            await client.aclose()
        only_local.conn.close()


def test_pair_command_prints_a_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARTHI_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["pair"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0
    assert "/start " in result.stdout
    assert "expires in 15 minutes" in result.stdout


def test_bot_command_explains_a_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARTHI_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["run"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "SARTHI_TELEGRAM_BOT_TOKEN" in result.output


class SilentApi:
    """Stands in for Telegram in end-to-end app tests."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.files: list[str] = []

    async def send_message(self, chat_id: int, text: str, markup: Any = None) -> int:
        self.sent.append(text)
        return len(self.sent)

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str = "", mime: str = ""
    ) -> int:
        self.files.append(filename)
        return 1

    async def aclose(self) -> None:
        return None


def quiet_app(tmp_path: Path, clock: Any = None, **settings: Any) -> Any:
    base = {
        "_env_file": None,
        "data_dir": tmp_path,
        "telegram_bot_token": SecretStr("1:x"),
        "llm_fast_models": [],
        "llm_quality_models": [],
        "job_boards": [],
        "feeds": [],
        "freehire": False,
    }
    sarthi = build(Settings(**(base | settings)), **({"clock": clock} if clock else {}))
    api = SilentApi()
    sarthi.bot.api = api  # type: ignore[assignment]
    sarthi.bot.catchup.api = api  # type: ignore[assignment]
    return sarthi, api


async def close(sarthi: Any) -> None:
    for client in sarthi.ai_clients:
        await client.aclose()
    sarthi.conn.close()


async def test_catchup_needs_pairing_and_a_profile(tmp_path: Path) -> None:
    sarthi, _ = quiet_app(tmp_path)
    try:
        assert "pair" in (sarthi.catchup_problem() or "")
        sarthi.bot.chats.set_state("telegram.owner_chat_id", "111")
        assert "/setup" in (sarthi.catchup_problem() or "")
        save_profile(profile_path(tmp_path), make_profile())
        assert sarthi.catchup_problem() is None
    finally:
        await close(sarthi)


async def test_one_shot_catchup_sends_the_batch_and_exits(tmp_path: Path) -> None:
    sarthi, api = quiet_app(tmp_path)
    sarthi.bot.chats.set_state("telegram.owner_chat_id", "111")
    save_profile(profile_path(tmp_path), make_profile())
    assert await sarthi.run_catchup(48, timeout=timedelta(seconds=30))
    assert any("Catch-up: last 2 days" in text for text in api.sent)


@pytest.mark.parametrize(
    ("away", "paired", "with_profile", "started"),
    [
        (timedelta(days=3), True, True, True),
        (timedelta(days=30), True, True, True),  # capped at 7 days
        (timedelta(hours=2), True, True, False),  # a short break: normal alerts
        (timedelta(days=3), False, True, False),
        (timedelta(days=3), True, False, False),
    ],
)
async def test_catchup_after_the_computer_was_off(
    tmp_path: Path, clock: FakeClock, away: timedelta, paired: bool, with_profile: bool, started: bool
) -> None:
    sarthi, api = quiet_app(tmp_path, clock)
    try:
        sarthi.bot.chats.set_state(ALIVE_KEY, to_iso(clock() - away))
        if paired:
            sarthi.bot.chats.set_state("telegram.owner_chat_id", "111")
        if with_profile:
            save_profile(profile_path(tmp_path), make_profile())
        assert await sarthi.catch_up_after_absence() is started
        assert sarthi.queue.active("catchup") == int(started)
        if started:
            assert "Welcome back" in api.sent[-1]
            assert ("7 days" in api.sent[-1]) is (away > timedelta(days=7))
    finally:
        await close(sarthi)


@pytest.mark.parametrize("value", [None, "garbage"])
async def test_no_catchup_without_a_valid_last_run(tmp_path: Path, value: str | None) -> None:
    sarthi, _ = quiet_app(tmp_path)
    try:
        sarthi.bot.chats.set_state("telegram.owner_chat_id", "111")
        save_profile(profile_path(tmp_path), make_profile())
        sarthi.bot.chats.set_state(ALIVE_KEY, value)
        assert not await sarthi.catch_up_after_absence()
    finally:
        await close(sarthi)


def test_catchup_command_explains_what_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARTHI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SARTHI_TELEGRAM_BOT_TOKEN", "1:x")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["catchup", "--days", "2"])
        too_far = CliRunner().invoke(cli, ["catchup", "--days", "30"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "pair" in result.output
    assert too_far.exit_code != 0
