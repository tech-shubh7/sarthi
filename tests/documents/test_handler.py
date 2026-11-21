"""The documents task end to end with a fake AI, and their delivery on Telegram."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from sarthi.ai.client import AiUnavailableError
from sarthi.ai.documents import ClaimCheck, CoverAnswer, TailorAnswer, TailoredJob
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.db.chat import ChatStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.documents.handler import LAST_TRY, NO_AI, NO_RESUME, DocumentMaker
from sarthi.profile.master import MasterResume
from sarthi.profile.models import Preferences, Profile
from sarthi.queue import names
from sarthi.queue.tasks import RetryableError, TaskQueue
from tests.chat.test_bot import OWNER, pair
from tests.chat.test_catchup import FileApi
from tests.conftest import FakeClock
from tests.documents.test_documents import RESUME_TEXT, master
from tests.matching.conftest import make_job


class FakeAi:
    """Answers each prompt by name; ``None`` means the provider is busy."""

    def __init__(self, answers: dict[str, BaseModel | None], available: bool = True) -> None:
        self.answers = answers
        self.available = available
        self.asked: list[str] = []

    async def structured(self, prompt: Any, schema: Any, **data: Any) -> Any:
        self.asked.append(prompt.name)
        answer = self.answers.get(prompt.name)
        if answer is None:
            raise AiUnavailableError("busy")
        return answer


def good_answers() -> dict[str, BaseModel | None]:
    return {
        "master_resume": master().model_copy(
            update={"contact": master().contact.model_copy(update={"email": ""})}
        ),
        "tailor_resume": TailorAnswer(
            headline="AI Backend Engineer",
            summary="Backend engineer building Python services and RAG search.",
            experience=[TailoredJob(index=0, bullets=["Added a RAG search with LangChain and pgvector"])],
        ),
        "cover_letter": CoverAnswer(
            paragraphs=[
                "I am applying for the Backend Engineer role.",
                "At Acme Labs I built 12 FastAPI services.",
            ]
        ),
        "check_claims": ClaimCheck(unsupported=[]),
    }


def profile(resume: str = RESUME_TEXT) -> Profile:
    prefs = Preferences(roles=("Backend Engineer",), home_country="IN", years_experience=1)
    return Profile(preferences=prefs, skills=("Python", "FastAPI"), resume_text=resume)


@pytest.fixture
def documents(conn: sqlite3.Connection, clock: FakeClock) -> DocumentStore:
    return DocumentStore(conn, clock=clock)


def maker(
    job_store: JobStore,
    documents: DocumentStore,
    queue: TaskQueue,
    tmp_path: Path,
    ai: FakeAi,
    who: Profile | None,
) -> DocumentMaker:
    return DocumentMaker(job_store, documents, queue, lambda: who, ai=ai, data_dir=tmp_path)  # type: ignore[arg-type]


def saved(job_store: JobStore) -> str:
    posting = make_job(description="Python, FastAPI, Docker and Kubernetes. RAG a plus.", company="Gamma AI")
    return job_store.save(posting).job_id


def ready(queue: TaskQueue) -> TaskRecord:
    task = queue.claim([names.DOCUMENTS_READY])
    assert task is not None
    return task


async def test_documents_are_made_stored_and_announced(
    job_store: JobStore, documents: DocumentStore, queue: TaskQueue, tmp_path: Path
) -> None:
    ai = FakeAi(good_answers())
    job_id = saved(job_store)
    await maker(job_store, documents, queue, tmp_path, ai, profile()).handle(
        TaskRecord(1, names.DOCUMENTS, job_id, {"apply": True}, 1)
    )
    record = documents.get(job_id)
    assert record is not None
    assert (tmp_path / record.resume_file).read_bytes().startswith(b"%PDF")
    assert (tmp_path / record.letter_file).name == "Asha_Rao_Gamma_AI_Cover_Letter.pdf"
    assert (
        (tmp_path / "documents" / job_id / "cover_letter.txt")
        .read_text()
        .startswith("Dear Gamma AI hiring team,")
    )
    report = record.report
    assert report["tailored_by_ai"] and report["letter_by_ai"]
    assert report["ats"] == []
    assert report["keywords"]["missing"] == ["Kubernetes"]
    task = ready(queue)
    assert task.payload["apply"] is True
    assert task.payload["error"] is None
    saved_master = MasterResume.model_validate_json((tmp_path / "master_resume.json").read_text())
    assert saved_master.contact.email == "asha.rao@example.com"  # read locally, never from the AI


async def test_master_is_read_once_per_uploaded_resume(
    job_store: JobStore, documents: DocumentStore, queue: TaskQueue, tmp_path: Path
) -> None:
    ai = FakeAi(good_answers())
    job_id = saved(job_store)
    first = maker(job_store, documents, queue, tmp_path, ai, profile())
    await first.make(job_id, profile())
    await first.make(job_id, profile())
    assert ai.asked.count("master_resume") == 1
    await first.make(job_id, profile(RESUME_TEXT + "\nAwards\n- Hackathon winner"))
    assert ai.asked.count("master_resume") == 2


async def test_a_busy_ai_is_waited_for_until_the_last_try(
    job_store: JobStore, documents: DocumentStore, queue: TaskQueue, tmp_path: Path
) -> None:
    answers = good_answers() | {"cover_letter": None}
    job_id = saved(job_store)
    make = maker(job_store, documents, queue, tmp_path, FakeAi(answers), profile())
    waits = []
    for attempt in range(1, LAST_TRY):
        with pytest.raises(RetryableError) as caught:
            await make.handle(TaskRecord(1, names.DOCUMENTS, job_id, {}, attempt))
        waits.append(caught.value.retry_after)
    # Longer each time (a free tier's minute, then its day, resets): not five tries in a few minutes.
    assert waits == [90.0, 360.0, 1440.0, 5760.0]
    await make.handle(TaskRecord(1, names.DOCUMENTS, job_id, {}, LAST_TRY))
    record = documents.get(job_id)
    assert record is not None
    assert record.report["letter_by_ai"] is False  # the plain letter, built from the resume alone


@pytest.mark.parametrize(
    ("who", "ai", "message"),
    [
        (profile(""), FakeAi(good_answers()), NO_RESUME),
        (profile(), FakeAi({}, available=False), NO_AI),
    ],
)
async def test_problems_are_told_to_the_user(
    job_store: JobStore,
    documents: DocumentStore,
    queue: TaskQueue,
    tmp_path: Path,
    who: Profile,
    ai: FakeAi,
    message: str,
) -> None:
    job_id = saved(job_store)
    await maker(job_store, documents, queue, tmp_path, ai, who).handle(
        TaskRecord(1, names.DOCUMENTS, job_id, {}, 1)
    )
    assert ready(queue).payload["error"] == message
    assert documents.get(job_id) is None


async def test_a_deleted_job_is_told_not_retried(
    job_store: JobStore, documents: DocumentStore, queue: TaskQueue, tmp_path: Path
) -> None:
    await maker(job_store, documents, queue, tmp_path, FakeAi(good_answers()), profile()).handle(
        TaskRecord(1, names.DOCUMENTS, "gone", {}, 1)
    )
    assert ready(queue).payload["error"] == "That job is no longer stored."


# ---- delivery on Telegram -------------------------------------------------------------------------------


@pytest.fixture
def bot(
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    documents: DocumentStore,
    tmp_path: Path,
    clock: FakeClock,
) -> TelegramBot:
    return TelegramBot(
        FileApi(),  # type: ignore[arg-type]
        chats,
        job_store,
        MatchStore(conn, clock=clock),
        queue,
        documents=documents,
        data_dir=tmp_path,
        clock=clock,
    )


@pytest.fixture
def chats(conn: sqlite3.Connection, clock: FakeClock) -> ChatStore:
    return ChatStore(conn, clock=clock)


async def test_ready_documents_are_sent_then_the_application_is_queued(
    bot: TelegramBot,
    chats: ChatStore,
    clock: FakeClock,
    job_store: JobStore,
    documents: DocumentStore,
    queue: TaskQueue,
    tmp_path: Path,
) -> None:
    pair(bot, chats, clock)
    job_id = saved(job_store)
    await maker(job_store, documents, queue, tmp_path, FakeAi(good_answers()), profile()).make(
        job_id, profile()
    )
    await bot.handle_documents_ready(
        TaskRecord(1, names.DOCUMENTS_READY, job_id, {"apply": True, "error": None}, 1)
    )
    api: Any = bot.api
    assert [name for name, _, _ in api.files] == [
        "Asha_Rao_Gamma_AI_Resume.pdf",
        "Asha_Rao_Gamma_AI_Cover_Letter.pdf",
    ]
    assert "Tailored for" in api.texts()[-1]
    assert queue.active(names.FORM_CHECK) == 1


async def test_errors_and_missing_files_are_explained(
    bot: TelegramBot,
    chats: ChatStore,
    clock: FakeClock,
    job_store: JobStore,
    documents: DocumentStore,
    queue: TaskQueue,
    tmp_path: Path,
) -> None:
    pair(bot, chats, clock)
    api: Any = bot.api
    await bot.handle_documents_ready(
        TaskRecord(1, names.DOCUMENTS_READY, "x", {"error": "<b>no</b> resume"}, 1)
    )
    assert api.texts()[-1] == "📄 &lt;b&gt;no&lt;/b&gt; resume"
    job_id = saved(job_store)
    await maker(job_store, documents, queue, tmp_path, FakeAi(good_answers()), profile()).make(
        job_id, profile()
    )
    record = documents.get(job_id)
    assert record is not None
    (tmp_path / record.resume_file).unlink()
    await bot.handle_documents_ready(
        TaskRecord(1, names.DOCUMENTS_READY, job_id, {"apply": True, "error": None}, 1)
    )
    assert "missing" in api.texts()[-1]
    assert queue.active(names.FORM_CHECK) == 0


async def test_docs_button_makes_documents_without_applying(
    bot: TelegramBot, chats: ChatStore, clock: FakeClock, job_store: JobStore, queue: TaskQueue
) -> None:
    pair(bot, chats, clock)
    job_id = saved(job_store)
    await bot.handle_update(
        {
            "callback_query": {
                "id": "c",
                "data": f"docs:{job_id}",
                "message": {"chat": {"id": OWNER}, "message_id": 5},
            }
        }
    )
    task = queue.claim([names.DOCUMENTS])
    assert task is not None
    assert task.payload == {"apply": False}
    assert chats.job_status(job_id) is None  # not an answer to the job


async def test_letter_and_summary_with_unsupported_claims_are_not_used(
    job_store: JobStore, documents: DocumentStore, queue: TaskQueue, tmp_path: Path
) -> None:
    answers = good_answers() | {
        "check_claims": ClaimCheck(unsupported=["Node.js microservices for a fintech product"])
    }
    ai = FakeAi(answers)
    job_id = saved(job_store)
    report = await maker(job_store, documents, queue, tmp_path, ai, profile()).make(job_id, profile())
    assert report["letter_by_ai"] is False  # two tries, both with a merged claim: the plain letter
    assert ai.asked.count("cover_letter") == 2
    assert any(line.startswith("summary:") for line in report["kept_original"])
    stored = documents.get(job_id)
    assert stored is not None
    text = (tmp_path / "documents" / job_id / "cover_letter.txt").read_text()
    assert (
        "I am applying for the Python Backend Engineer role at Gamma AI." in text
        or "role at Gamma AI" in text
    )


async def test_all_caps_names_are_shown_normally(
    job_store: JobStore, documents: DocumentStore, queue: TaskQueue, tmp_path: Path
) -> None:
    answers = good_answers() | {"master_resume": master().model_copy(update={"name": "ASHA RAO"})}
    made = await maker(job_store, documents, queue, tmp_path, FakeAi(answers), profile()).master(profile())
    assert made.name == "Asha Rao"
