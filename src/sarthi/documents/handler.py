"""The ``documents`` task: a tailored resume, a cover letter and an ATS keyword report for one job.

Made only for jobs the user tapped Apply (or "Tailored resume") on, not for every match, so AI calls stay few.
Steps: master resume (read once per uploaded resume, kept only where the resume text confirms it) → tailoring
(AI words, code facts; one more try when the AI used clichés, then strict) → cover letter (same) → PDFs →
read-back check → keyword report → a ``documents_ready`` task for the chat.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sarthi.ai.client import AiClient
from sarthi.ai.documents import cover_letter as ai_cover_letter
from sarthi.ai.documents import read_master, tailor, unsupported_claims
from sarthi.clock import Clock, utcnow
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.documents.build import Letter, cover_letter, plain_letter, tailored_resume, without_claims
from sarthi.documents.keywords import keyword_report
from sarthi.documents.render import (
    RenderError,
    ats_problems,
    letter_pdf,
    paper_for,
    pdf_text,
    resume_pdf,
    write,
)
from sarthi.profile.master import (
    MasterResume,
    contact_from,
    grounded,
    load_master,
    master_path,
    name_from,
    save_master,
)
from sarthi.profile.models import Profile
from sarthi.queue import names
from sarthi.queue.tasks import MAX_ATTEMPTS, DeferError, PermanentError, Priority, RetryableError, TaskQueue

log = logging.getLogger(__name__)

WAIT_FOR_PROFILE_S = 3600
AI_BUSY_WAIT_S = 90.0  # free AI tiers limit tokens per minute: better documents after a short wait
BUSY_BACKOFF = 4
LAST_TRY = MAX_ATTEMPTS  # on the queue's last attempt, the plain fallbacks are accepted
DOCUMENTS_DIR = "documents"
NO_RESUME = "Tailored documents need your resume: send /setup and upload it."
NO_AI = "Tailored documents need an AI model: set SARTHI_LLM_QUALITY_MODELS (see .env.example)."


@dataclass(frozen=True, slots=True)
class Tries:
    """Which attempt of the task this is; a busy AI is waited for longer each time.

    Free tiers reset per minute, then per day: 90 s, 6 min, 24 min, 1.6 h. The last attempt takes what it can.
    """

    attempt: int

    @property
    def last(self) -> bool:
        """True on the queue's last attempt: the plain fallbacks are accepted."""
        return self.attempt >= LAST_TRY

    @property
    def wait(self) -> float:
        """Seconds to wait before the next attempt when the AI is busy."""
        return AI_BUSY_WAIT_S * float(BUSY_BACKOFF) ** max(0, self.attempt - 1)


def resume_hash(text: str) -> str:
    """Identifies the uploaded resume a master resume was read from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_stem(name: str, company: str) -> str:
    """A file name part like "Asha_Rao_Acme": safe on every file system, readable for recruiters."""
    parts = [re.sub(r"[^A-Za-z0-9]+", "_", part).strip("_") for part in (name, company)]
    return "_".join(p for p in parts if p)[:80] or "Sarthi"


class DocumentMaker:
    """Handles ``documents`` tasks."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        jobs: JobStore,
        documents: DocumentStore,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        *,
        ai: AiClient,
        data_dir: Path,
        clock: Clock = utcnow,
    ) -> None:
        """Read jobs and the profile; write files under ``data_dir``; ask ``ai`` (the quality models)."""
        self.jobs = jobs
        self.documents = documents
        self.queue = queue
        self._load_profile = load_profile
        self.ai = ai
        self.data_dir = data_dir
        self._clock = clock

    async def master(self, profile: Profile) -> MasterResume:
        """The master resume for the current upload, read by the AI once and then kept."""
        text = profile.resume_text
        if not text.strip():
            raise PermanentError(NO_RESUME)
        path = master_path(self.data_dir)
        saved = load_master(path)
        if saved is not None and saved.source_hash == resume_hash(text):
            return saved
        if not self.ai.available:
            raise PermanentError(NO_AI)
        draft = await read_master(self.ai, text)
        if draft is None:
            raise RetryableError("the AI could not read the resume into sections right now")
        master = grounded(draft, text).model_copy(
            update={"contact": contact_from(text), "source_hash": resume_hash(text)}
        )
        name = master.name or name_from(text)
        master = master.model_copy(update={"name": name.title() if name.isupper() else name})  # "ASHA RAO"
        save_master(path, master)
        return master

    async def handle(self, task: TaskRecord) -> None:
        """Make the documents for the job named by ``task.key``; always tell the chat how it went."""
        profile = self._load_profile()
        if profile is None:
            raise DeferError(WAIT_FOR_PROFILE_S)
        try:
            report = await self.make(task.key, profile, tries=Tries(task.attempts))
        except PermanentError as error:
            self._ready(task, {"error": str(error)})
            return
        self._ready(task, {"error": None, **report})

    def _ready(self, task: TaskRecord, result: dict[str, Any]) -> None:
        self.queue.enqueue(
            names.DOCUMENTS_READY, task.key, {**task.payload, **result}, priority=Priority.ALERT
        )

    def _busy(self, answer: object, tries: Tries) -> None:
        """No AI answer although a model is set up: wait and try again, unless this is the last try."""
        if answer is None and self.ai.available and not tries.last:
            raise RetryableError("the AI is busy (rate limit); trying again later", retry_after=tries.wait)

    async def make(self, job_id: str, profile: Profile, *, tries: Tries | None = None) -> dict[str, Any]:
        """Write the files and store the record. Returns the report.

        Unless this is the last try, a missing AI answer raises ``RetryableError``, so the task waits for the
        AI rather than settling for the untailored resume or the plain letter. ``tries`` None: a last try.
        """
        tries = tries or Tries(LAST_TRY)
        job = self.jobs.get(job_id)
        if job is None:
            raise PermanentError("That job is no longer stored.")
        job_text = f"{job.title}\n{job.company}\n\n{self.jobs.full_text(job_id) or job.description}"
        master = await self.master(profile)
        skills = profile.skills

        answer = await tailor(self.ai, master, job_text)
        self._busy(answer, tries)
        built = tailored_resume(master, answer, profile.resume_text, skills)
        if built.problems:  # clichés or AI talk: one more try with the problems listed, then strict
            answer = await tailor(self.ai, master, job_text, built.problems[:10]) or answer
            built = tailored_resume(master, answer, profile.resume_text, skills, strict=True)

        built = await self._checked_summary(master, built, tries)
        letter = await self._letter(
            master, profile, role=job.title, company=job.company, job_text=job_text, tries=tries
        )

        paper = paper_for(profile.preferences.home_country)
        try:
            resume_bytes = resume_pdf(built.resume, paper)
            letter_bytes = letter_pdf(built.resume, letter, paper, today=self._today())
        except RenderError as error:
            raise PermanentError(f"The PDF could not be made: {error}") from error

        stem = file_stem(master.name, job.company)
        folder = self.data_dir / DOCUMENTS_DIR / job_id
        resume_file, letter_file = folder / f"{stem}_Resume.pdf", folder / f"{stem}_Cover_Letter.pdf"
        write(resume_file, resume_bytes)
        write(letter_file, letter_bytes)
        write(folder / "cover_letter.txt", letter.text().encode("utf-8"))

        read_back, _ = pdf_text(resume_bytes)
        keywords = keyword_report(job_text, read_back, f"{profile.resume_text}\n{', '.join(skills)}", skills)
        report: dict[str, Any] = {
            "role": job.title,
            "company": job.company,
            "keywords": {
                "on_resume": keywords.on_resume,
                "have_not_shown": keywords.have_not_shown,
                "missing": keywords.missing,
                "score": keywords.score,
            },
            "ats": ats_problems(resume_bytes, built.resume),
            "tailored_by_ai": answer is not None,
            "letter_by_ai": letter.from_ai,
            "kept_original": built.reverted[:10],
        }
        self.documents.save(
            DocumentRecord(
                job_id=job_id,
                profile_version=profile.version,
                resume_file=str(resume_file.relative_to(self.data_dir)),
                letter_file=str(letter_file.relative_to(self.data_dir)),
                report=report,
            )
        )
        log.info(
            "documents for %s at %s: keywords %s",
            job.title,
            job.company,
            "none listed" if keywords.score is None else f"{keywords.score}%",
        )
        return report

    async def _claims(self, master: MasterResume, text: str, tries: Tries) -> list[str]:
        """The AI reviewer's unsupported claims (waits for a busy AI unless this is the last try)."""
        found = await unsupported_claims(self.ai, master, text)
        self._busy(found, tries)
        return found or []

    async def _checked_summary(self, master: MasterResume, built: Any, tries: Tries) -> Any:
        """The tailored summary, or the master's when the reviewer finds a claim the resume lacks."""
        summary = built.resume.summary
        if summary == master.summary or not summary:
            return built
        problems = await self._claims(master, summary, tries)
        if problems:
            built.resume = built.resume.model_copy(update={"summary": master.summary})
            built.reverted += [f"summary: {p}" for p in problems[:3]]
        return built

    async def _letter(  # noqa: PLR0913  (the letter's inputs)
        self,
        master: MasterResume,
        profile: Profile,
        *,
        role: str,
        company: str,
        job_text: str,
        tries: Tries,
    ) -> Letter:
        """A letter that passes the code checks and the claim reviewer.

        The first answer's problems (a number or skill the resume lacks, a cliché, a claim the reviewer cannot
        find) go back to the AI once; from that second answer the true paragraphs are kept. The plain letter
        is the last resort.
        """
        problems: list[str] = []
        letter = Letter("", [], "", from_ai=False)
        for strict in (False, True):
            answer = await ai_cover_letter(self.ai, master, job_text, problems[:10] or None)
            self._busy(answer, tries)
            letter = cover_letter(
                master,
                answer,
                profile.resume_text,
                role=role,
                company=company,
                own_skills=profile.skills,
                strict=strict,
            )
            if not letter.from_ai:
                if answer is None or strict:
                    return letter
                problems = letter.problems  # untrue on the first try: ask again with what was wrong
                continue
            claims = await self._claims(master, "\n\n".join(letter.paragraphs), tries)
            if strict:
                if not claims:
                    return letter
                trimmed = without_claims(letter, claims)
                if trimmed is not None:
                    return trimmed
                break
            problems = letter.problems + [f"unsupported claim: {c}" for c in claims]
            if not problems:
                return letter
        return Letter(letter.greeting, plain_letter(master, role, company), letter.sign_off, from_ai=False)

    def _today(self) -> date:
        return self._clock().astimezone().date()
