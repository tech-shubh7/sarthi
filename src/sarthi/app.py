"""Builds Sarthi from settings and runs it: the only module that knows every part (composition root)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sarthi.ai.client import AiClient
from sarthi.ai.providers import resolve
from sarthi.ai.scoring import LlmMatchScorer
from sarthi.answers.handler import FormChecker
from sarthi.apply.engine import ApplyEngine
from sarthi.apply.handler import Applier
from sarthi.chat.pairing import owner_chat
from sarthi.chat.setup import SetupFlow
from sarthi.chat.telegram.api import TelegramApi
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.clock import Clock, from_iso, to_iso, utcnow
from sarthi.config import Settings
from sarthi.db.ai_store import AiStore
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.replies import ReplyStore
from sarthi.db.source_state import SourceStateStore
from sarthi.db.tasks import TaskRecord, TaskStore
from sarthi.documents.handler import DocumentMaker
from sarthi.matching.handler import LOGIC_VERSION, Matcher, match_version
from sarthi.profile.master import load_master, master_path
from sarthi.profile.models import Profile
from sarthi.profile.repair import repair
from sarthi.profile.store import load_profile, profile_path, save_profile
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, RetryableError, TaskQueue
from sarthi.queue.worker import Handler, Worker
from sarthi.sources.ingest import Ingestor
from sarthi.sources.schedule import SourceScheduler, plan_sources
from sarthi.tracking.handler import Mailbox, ReplyTracker

log = logging.getLogger(__name__)

WORKER_CONCURRENCY = 3
USER_CONCURRENCY = 2
BROWSER_PROFILE = "browser-profile"  # the user's site logins (LinkedIn, Naukri), kept on their computer
BACKGROUND_TASKS = frozenset(
    {names.SOURCE_CHECK, names.MATCH, names.CATCHUP, names.MAIL_CHECK, names.QUIET_CHECK}
)
MATCH_LOGIC_KEY = "matching.logic"
ALIVE_KEY = "app.alive"  # last time a running Sarthi was seen (ISO), for catch-up after the computer was off
AUTO_CATCHUP_AFTER = timedelta(hours=6)
CATCHUP_POLL_S = 2.0
REMATCH_AFTER_UPGRADE = timedelta(days=7)
TICK_SECONDS = 60


class SetupError(Exception):
    """Sarthi cannot start because something is not configured."""


def next_digest_at(now: datetime, hour: int) -> datetime:
    """The next time the local clock shows ``hour``:00 (today if still ahead, else tomorrow)."""
    local = now.astimezone()
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return target if target > local else target + timedelta(days=1)


@dataclass
class App:
    """Everything a running Sarthi needs, wired together."""

    settings: Settings
    conn: sqlite3.Connection
    queue: TaskQueue
    bot: TelegramBot
    matcher: Matcher
    sources: SourceScheduler
    documents: DocumentMaker
    forms: FormChecker
    applier: Applier
    tracker: ReplyTracker
    ai_clients: tuple[AiClient, ...]
    clock: Clock

    def schedule_digest(self) -> None:
        """Queue the next daily digest (idempotent: one task per local date)."""
        at = next_digest_at(self.clock(), self.settings.digest_hour)
        delay = (at - self.clock()).total_seconds()
        self.queue.enqueue(names.DIGEST, at.date().isoformat(), delay=delay, priority=Priority.NORMAL)

    async def _digest(self, task: TaskRecord) -> None:
        await self.bot.handle_digest(task)
        self.schedule_digest()

    def handlers(self) -> dict[str, Handler]:
        """Every task type and what runs it."""
        return {
            names.SOURCE_CHECK: self.sources.handle,
            names.MATCH: self.matcher.handle,
            names.CATCHUP: self.sources.handle_catchup,
            names.ALERT: self.bot.handle_alert,
            names.DIGEST: self._digest,
            names.CATCHUP_REPORT: self.bot.handle_catchup_report,
            names.DOCUMENTS: self.documents.handle,
            names.DOCUMENTS_READY: self.bot.handle_documents_ready,
            names.FORM_CHECK: self.forms.handle,
            names.ASK_QUESTIONS: self.bot.handle_ask_questions,
            names.APPLY: self.applier.handle_apply,
            names.SUBMIT: self.applier.handle_submit,
            names.APPLY_PREVIEW: self.bot.handle_apply_preview,
            names.APPLY_RESULT: self.bot.handle_apply_result,
            names.MAIL_CHECK: self.tracker.handle_mail,
            names.QUIET_CHECK: self.tracker.handle_quiet,
            names.REPLY_NOTICE: self.bot.handle_reply_notice,
            names.FOLLOWUP_NOTICE: self.bot.handle_followup_notice,
        }

    def worker(self) -> Worker:
        """Background work: reading sources and matching (slow, AI-heavy, never what the user waits on)."""
        handlers = {t: h for t, h in self.handlers().items() if t in BACKGROUND_TASKS}
        return Worker(self.queue, handlers, concurrency=WORKER_CONCURRENCY, name="background")

    def user_worker(self) -> Worker:
        """What the user asked for or waits on (alerts, documents, applying): never behind matching."""
        handlers = {t: h for t, h in self.handlers().items() if t not in BACKGROUND_TASKS}
        return Worker(self.queue, handlers, concurrency=USER_CONCURRENCY, name="user")

    async def _tick_sources(self, worker: Worker) -> None:
        while True:
            self.bot.chats.set_state(ALIVE_KEY, to_iso(self.clock()))
            await self.applier.engine.close_idle()  # forms nobody approved in time
            if self.sources.tick():
                worker.wake()
            await asyncio.sleep(TICK_SECONDS)

    async def catch_up_after_absence(self) -> bool:
        """When Sarthi was off for a while, start one catch-up over the gap (capped at 7 days)."""
        last = self.bot.chats.get_state(ALIVE_KEY)
        chat_id = owner_chat(self.bot.chats)
        if not last or chat_id is None or self.sources.profile() is None:
            return False
        try:
            gap = self.clock() - from_iso(last)
        except ValueError:
            return False
        if gap < AUTO_CATCHUP_AFTER:
            return False
        return await self.bot.catchup.start_after_absence(chat_id, math.ceil(gap.total_seconds() / 3600))

    def rematch_after_upgrade(self) -> int:
        """Redo recent matches once when the matching logic changed. Returns how many were queued."""
        chats, jobs = self.bot.chats, self.bot.jobs
        if chats.get_state(MATCH_LOGIC_KEY) == LOGIC_VERSION:
            return 0
        ids = jobs.ids_active_since(self.clock() - REMATCH_AFTER_UPGRADE)
        for job_id in ids:
            self.queue.enqueue(names.MATCH, job_id, priority=Priority.NORMAL)
        chats.set_state(MATCH_LOGIC_KEY, LOGIC_VERSION)
        log.info("matching logic changed: %d recent job(s) queued to be matched again", len(ids))
        return len(ids)

    async def run(self) -> None:
        """Run the bot, the source scheduler and the worker until cancelled."""
        self.queue.reclaim_stuck()
        self.rematch_after_upgrade()
        self.schedule_digest()
        self.tracker.schedule()
        self.tracker.schedule_quiet()
        try:
            await self.catch_up_after_absence()
        except (RetryableError, PermanentError) as error:
            log.warning("could not start the catch-up after a break: %s", error)
        worker, user = self.worker(), self.user_worker()
        tasks = [
            asyncio.create_task(self.bot.poll()),
            asyncio.create_task(worker.run()),
            asyncio.create_task(user.run()),
            asyncio.create_task(self._tick_sources(worker)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            await self._shutdown((worker, user), tasks)

    def catchup_problem(self) -> str | None:
        """Why a catch-up cannot run now (not paired, no profile), or None."""
        if owner_chat(self.bot.chats) is None:
            return "Telegram is not paired yet: run `uv run sarthi pair` first"
        if self.sources.profile() is None:
            return "no profile yet: send /setup to your Sarthi bot first"
        return None

    async def run_catchup(self, hours: int, timeout: timedelta = timedelta(hours=1)) -> bool:
        """One catch-up, then exit (for a computer that is on only part of the day, e.g. from cron).

        Returns False when the results could not be sent within ``timeout``.
        """
        self.queue.reclaim_stuck()
        self.rematch_after_upgrade()
        if not self.bot.catchup.start(hours):
            log.info("a catch-up is already running; waiting for it")
        worker, user = self.worker(), self.user_worker()
        tasks = [asyncio.create_task(worker.run()), asyncio.create_task(user.run())]
        deadline = self.clock() + timeout
        try:
            while self.clock() < deadline:
                await asyncio.sleep(CATCHUP_POLL_S)
                if not self.bot.catchup.running():
                    return True
            return False
        finally:
            await self._shutdown((worker, user), tasks)

    async def _shutdown(self, workers: tuple[Worker, ...], tasks: list[asyncio.Task[None]]) -> None:
        self.bot.stop()
        for worker in workers:
            worker.stop()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.bot.inbox is not None:
            await self.bot.inbox.close()
        await self.bot.api.aclose()
        await self.applier.engine.aclose()
        for client in self.ai_clients:
            await client.aclose()
        self.conn.close()


def _ai(
    settings: Settings,
    models: list[str],
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    background: bool = False,
) -> AiClient:
    """A client for ``models``; a ``background`` one uses no local model and gives way to the user's calls."""
    endpoints = resolve(models, settings)
    if background:
        endpoints = [endpoint for endpoint in endpoints if not endpoint.free]
    return AiClient(
        endpoints,
        AiStore(conn, clock=clock),
        daily_token_budget=settings.llm_daily_token_budget,
        requests_per_minute=settings.llm_requests_per_minute,
        clock=clock,
        background=background,
    )


def repair_saved_profile(path: Path) -> None:
    """Remove junk that older versions saved into the profile (e.g. "Use skills" as a skill)."""
    saved = load_profile(path)
    if saved is None:
        return
    fixed = repair(saved)
    if fixed is not saved:
        save_profile(path, fixed)
        log.info("cleaned leftover instructions from the saved profile's roles and skills")


def build(settings: Settings, clock: Clock = utcnow) -> App:
    """Wire Sarthi from ``settings``. Raises ``SetupError`` when the bot token is missing."""
    if settings.telegram_bot_token is None:
        raise SetupError("SARTHI_TELEGRAM_BOT_TOKEN is not set in .env (create a bot with @BotFather)")
    conn = connect(settings.db_path)
    queue = TaskQueue(TaskStore(conn, clock=clock), clock=clock)
    jobs, matches = JobStore(conn, clock=clock), MatchStore(conn, clock=clock)
    chats, states = ChatStore(conn, clock=clock), SourceStateStore(conn, clock=clock)
    fast, quality = (
        _ai(settings, settings.llm_fast_models, conn, clock),
        _ai(settings, settings.llm_quality_models, conn, clock),
    )
    # Matching scores every new job, thousands a day: a local model doing that keeps a laptop CPU at full
    # load, and on a free cloud tier it would use up the limits the user's documents need. It gets only the
    # cloud fast models, behind the user's calls; with none answering, jobs get the free score instead.
    matching_ai = _ai(settings, settings.llm_fast_models, conn, clock, background=True)
    path = profile_path(settings.data_dir)
    repair_saved_profile(path)

    def profile() -> Profile | None:
        return load_profile(path)

    api = TelegramApi(settings.telegram_bot_token)
    setup = SetupFlow(api, chats, quality, path, jobs=jobs, queue=queue, clock=clock)

    def profile_version() -> str | None:
        current = profile()
        return match_version(current) if current else None

    bot = TelegramBot(
        api,
        chats,
        jobs,
        matches,
        queue,
        setup=setup,
        group_messages=True,
        profile_version=profile_version,
        documents=DocumentStore(conn, clock=clock),
        answers=AnswerStore(conn, clock=clock),
        applications=ApplicationStore(conn, clock=clock),
        data_dir=settings.data_dir,
        clock=clock,
    )

    def chat_busy() -> bool:
        return bot.inbox is not None and bot.inbox.busy

    matcher = Matcher(
        jobs, matches, queue, profile, ai=LlmMatchScorer(matching_ai), ai_paused=chat_busy, clock=clock
    )
    planned = plan_sources(
        settings.job_boards,
        settings.watch_companies,
        timedelta(minutes=settings.boards_every_minutes),
        timedelta(minutes=settings.companies_every_minutes),
        feeds=settings.feeds,
        feeds_every=timedelta(minutes=settings.feeds_every_minutes),
        freehire=settings.freehire,
    )
    sources = SourceScheduler(planned, Ingestor(jobs, queue, states), states, queue, profile, clock=clock)
    documents = DocumentMaker(
        jobs,
        DocumentStore(conn, clock=clock),
        queue,
        profile,
        ai=quality,
        data_dir=settings.data_dir,
        clock=clock,
    )
    forms = FormChecker(
        jobs,
        DocumentStore(conn, clock=clock),
        AnswerStore(conn, clock=clock),
        queue,
        profile,
        data_dir=settings.data_dir,
    )
    applier = Applier(
        ApplyEngine(
            settings.data_dir / "screenshots",
            profile_dir=settings.data_dir / BROWSER_PROFILE,
            headless=settings.apply_headless,
        ),
        jobs,
        DocumentStore(conn, clock=clock),
        AnswerStore(conn, clock=clock),
        ApplicationStore(conn, clock=clock),
        queue=queue,
        load_profile=profile,
        data_dir=settings.data_dir,
        daily_limit=settings.apply_daily_limit,
        clock=clock,
    )
    mailbox = (
        Mailbox(
            settings.mail_imap_host,
            settings.mail_imap_port,
            settings.mail_user,
            settings.mail_password.get_secret_value(),
            settings.mail_folder,
        )
        if settings.mail_enabled and settings.mail_password is not None
        else None
    )

    def own_name() -> str:
        master = load_master(master_path(settings.data_dir))
        return master.name if master else ""

    tracker = ReplyTracker(
        ReplyStore(conn, clock=clock),
        ApplicationStore(conn, clock=clock),
        jobs,
        chats,
        queue,
        ai=fast,
        mailbox=mailbox,
        every=timedelta(minutes=settings.mail_every_minutes),
        followup_after=timedelta(days=settings.followup_after_days),
        name=own_name,
        clock=clock,
    )
    return App(
        settings,
        conn,
        queue,
        bot,
        matcher,
        sources,
        documents,
        forms,
        applier,
        tracker,
        (fast, quality, matching_ai),
        clock,
    )
