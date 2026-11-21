import sqlite3
from typing import Any

import pytest

from sarthi.chat.pairing import new_code
from sarthi.chat.telegram.api import Markup
from sarthi.chat.telegram.bot import APPLY_TASK, HELP, PAUSED_KEY, TelegramBot
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue.tasks import DeferError, PermanentError, TaskQueue
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

OWNER = 111
STRANGER = 666


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Markup | None]] = []
        self.edits: list[tuple[int, int, Markup | None]] = []
        self.toasts: list[str] = []
        self.updates: list[list[dict[str, Any]]] = []

    async def send_message(self, chat_id: int, text: str, markup: Markup | None = None) -> int:
        self.sent.append((chat_id, text, markup))
        return 1000 + len(self.sent)

    async def edit_buttons(self, chat_id: int, message_id: int, markup: Markup | None) -> None:
        self.edits.append((chat_id, message_id, markup))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.toasts.append(text)

    async def set_commands(self, commands: tuple[tuple[str, str], ...]) -> None:
        self.commands = commands

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        return self.updates.pop(0) if self.updates else []

    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def chats(conn: sqlite3.Connection, clock: FakeClock) -> ChatStore:
    return ChatStore(conn, clock=clock)


@pytest.fixture
def bot(
    api: FakeApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> TelegramBot:
    return TelegramBot(api, chats, job_store, MatchStore(conn, clock=clock), queue, clock=clock)  # type: ignore[arg-type]


def pair(bot: TelegramBot, chats: ChatStore, clock: FakeClock) -> None:
    chats.set_state("telegram.owner_chat_id", str(OWNER))


def message(chat_id: int, text: str) -> dict[str, Any]:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def tap(chat_id: int, data: str, message_id: int = 1001) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
        }
    }


def saved_job(bot: TelegramBot, score: int = 80, decision: str = "alert", n: int = 1) -> str:
    job_id = bot.jobs.save(
        make_job(url=f"https://boards.greenhouse.io/razorpay/jobs/{n}", title=f"Backend Engineer {n}")
    ).job_id
    bot.matches.save(
        MatchRecord(job_id, "v", score, decision, "strong", None, ("Mentions python",), (), False)
    )
    return job_id


async def test_before_pairing_everyone_gets_instructions(
    bot: TelegramBot, api: FakeApi, chats: ChatStore
) -> None:
    for text in ("/start", "/help", "/status"):
        await bot.handle_update(message(OWNER, text))
    assert all("uv run sarthi pair" in reply for reply in api.texts())
    assert len(api.sent) == 3


@pytest.mark.parametrize("form", ["/start {code}", "{code}", "  {code} "])
async def test_pairing_with_or_without_start(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock, form: str
) -> None:
    code = new_code(chats, clock())
    await bot.handle_update(message(OWNER, "/start"))  # the Start button sends no code
    await bot.handle_update(message(OWNER, form.format(code=code.lower())))
    assert "Paired" in api.texts()[-1]
    await bot.handle_update(message(OWNER, "/help"))
    assert "/today" in api.texts()[-1]


async def test_wrong_code_explains_before_pairing_but_is_silent_after(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    new_code(chats, clock())
    await bot.handle_update(message(STRANGER, "NOTTHECODE00"))
    assert "didn't work" in api.texts()[-1]
    pair(bot, chats, clock)
    sent = len(api.sent)
    await bot.handle_update(message(STRANGER, "/start NOTTHECODE00"))
    assert len(api.sent) == sent


async def test_running_pair_again_moves_sarthi_to_a_new_chat(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    code = new_code(chats, clock())
    await bot.handle_update(message(222, f"/start {code}"))
    assert chats.get_state("telegram.owner_chat_id") == "222"


async def test_strangers_get_no_reply(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    for text in ("/status", "/start", "hello", "/today"):
        await bot.handle_update(message(STRANGER, text))
    await bot.handle_update(tap(STRANGER, "apply:anything"))
    assert api.sent == []
    assert api.edits == []


async def test_alert_sends_a_card_once(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    job_id = saved_job(bot)
    with pytest.raises(DeferError):
        await bot.handle_alert(TaskRecord(1, "alert", job_id, {}, 1))  # not paired yet
    pair(bot, chats, clock)
    await bot.handle_alert(TaskRecord(1, "alert", job_id, {}, 1))
    await bot.handle_alert(TaskRecord(1, "alert", job_id, {}, 2))  # a retried task never sends twice
    assert len(api.sent) == 1
    chat_id, text, markup = api.sent[0]
    assert chat_id == OWNER
    assert "Backend Engineer 1" in text
    assert "Match: <b>80%</b>" in text
    assert markup is not None
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == [
        f"apply:{job_id}",
        f"skip:{job_id}",
        f"later:{job_id}",
        f"more:{job_id}",
        f"docs:{job_id}",
    ]
    assert chats.job_status(job_id) is JobStatus.ALERTED
    assert chats.message_id(job_id) == 1001


async def test_paused_alerts_wait_for_today(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    job_id = saved_job(bot)
    await bot.handle_update(message(OWNER, "/pause"))
    await bot.handle_alert(TaskRecord(1, "alert", job_id, {}, 1))
    assert chats.get_state(PAUSED_KEY) == "1"
    assert len(api.sent) == 1  # only the "paused" reply
    await bot.handle_update(message(OWNER, "/resume"))
    assert "1 match(es) waiting" in api.texts()[-1]
    await bot.handle_update(message(OWNER, "/today"))
    assert "1 match for you" in api.texts()[-2]
    assert chats.job_status(job_id) is JobStatus.DIGESTED


async def test_apply_queues_once_and_first_answer_wins(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    job_id = saved_job(bot)
    await bot.handle_alert(TaskRecord(1, "alert", job_id, {}, 1))
    await bot.handle_update(tap(OWNER, f"apply:{job_id}"))
    await bot.handle_update(tap(OWNER, f"apply:{job_id}"))
    await bot.handle_update(tap(OWNER, f"skip:{job_id}"))
    assert chats.job_status(job_id) is JobStatus.APPLY
    assert api.toasts == ["✅ Apply queued"] * 3
    assert api.edits[-1] == (
        OWNER,
        1001,
        {"inline_keyboard": [[{"text": "✅ Apply queued", "callback_data": "noop"}]]},
    )
    task = bot.queue.claim(["documents"])  # the tailored resume first, then the application
    assert task is not None
    assert (task.key, task.payload) == (job_id, {"apply": True})
    assert bot.queue.claim(["documents"]) is None


@pytest.mark.parametrize(("action", "status"), [("skip", JobStatus.SKIPPED), ("later", JobStatus.LATER)])
async def test_skip_and_later(
    bot: TelegramBot, chats: ChatStore, clock: FakeClock, action: str, status: JobStatus
) -> None:
    pair(bot, chats, clock)
    job_id = saved_job(bot)
    await bot.handle_update(tap(OWNER, f"{action}:{job_id}"))
    assert chats.job_status(job_id) is status
    assert bot.queue.claim([APPLY_TASK]) is None


async def test_details_and_unknown_buttons(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    job_id = bot.jobs.save(make_job(), full_text="Build payment APIs <in Go>.").job_id
    await bot.handle_update(tap(OWNER, f"more:{job_id}"))
    assert "Build payment APIs &lt;in Go&gt;." in api.texts()[-1]
    await bot.handle_update(tap(OWNER, "more:missing"))
    assert "no longer stored" in api.texts()[-1]
    await bot.handle_update(tap(OWNER, "noop"))
    assert len(api.sent) == 2


async def test_commands(bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock) -> None:
    pair(bot, chats, clock)
    saved_job(bot, decision="digest", score=50)
    for text in ("/help", "/status", "/today@SarthiBot", "/today", "what?"):
        await bot.handle_update(message(OWNER, text))
    texts = api.texts()
    assert "/today" in texts[0]
    assert "Jobs found: 1" in texts[1]
    assert "fair: 1" in texts[1]
    assert "1 match for you" in texts[2]
    assert "No new matches right now" in texts[4]
    assert "didn't understand" in texts[5]


async def test_digest_is_quiet_when_nothing_is_new(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    with pytest.raises(DeferError):
        await bot.handle_digest(TaskRecord(1, "digest", "d", {}, 1))
    pair(bot, chats, clock)
    await bot.handle_digest(TaskRecord(1, "digest", "d", {}, 1))
    assert api.sent == []
    for n in range(12):
        saved_job(bot, n=n, score=40 + n)
    await bot.handle_digest(TaskRecord(1, "digest", "d", {}, 1))
    assert api.texts()[0] == "<b>10 matches for you</b>"
    assert "Backend Engineer 11" in api.texts()[1]  # best first


async def test_poll_handles_updates_and_remembers_the_offset(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    api.updates = [[{"update_id": 7, **message(OWNER, "/help")}]]
    original = api.get_updates

    async def stop_after_first(offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        result = await original(offset, timeout)
        if not api.updates:
            bot.stop()
        return result

    api.get_updates = stop_after_first  # type: ignore[method-assign]
    await bot.poll()
    assert chats.get_state("telegram.offset") == "8"
    assert len(api.sent) == 1


async def test_poll_sets_the_slash_command_menu(bot: TelegramBot, api: FakeApi) -> None:
    bot.stop()
    await bot.poll()
    names = [name for name, _ in api.commands]
    assert names == [
        "setup",
        "profile",
        "today",
        "catchup",
        "watch",
        "watching",
        "status",
        "pause",
        "resume",
        "cancel",
        "help",
    ]
    assert all(
        n.islower() and 1 <= len(n) <= 32 and len(d) <= 256 for n, d in api.commands
    )  # Telegram's limits
    for name in names:
        assert f"/{name} – " in HELP


async def test_menu_failure_does_not_stop_the_bot(bot: TelegramBot, api: FakeApi) -> None:
    async def refuse(commands: tuple[tuple[str, str], ...]) -> None:
        raise PermanentError("telegram setMyCommands: Bad Request")

    api.set_commands = refuse  # type: ignore[method-assign]
    bot.stop()
    await bot.poll()
