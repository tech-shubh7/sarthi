from datetime import timedelta
from typing import Any

import pytest

from sarthi.ai.scoring import AiScore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.models import JobPosting
from sarthi.matching.decide import Decision, Thresholds, decide
from sarthi.matching.funnel import evaluate
from sarthi.matching.handler import ALERT_TASK, Matcher, match_version
from sarthi.matching.rules import RulesResult
from sarthi.profile.models import Profile
from sarthi.queue.tasks import DeferError, PermanentError, TaskQueue
from tests.conftest import FakeClock
from tests.matching.conftest import NOW, make_job, make_profile


class FakeAi:
    def __init__(self, verdict: AiScore | None, *, available: bool = True) -> None:
        self.verdict = verdict
        self.available = available
        self.calls = 0

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        self.calls += 1
        return self.verdict


@pytest.mark.parametrize(
    ("rules", "score", "confidence", "decision", "rule"),
    [
        (
            RulesResult(eligible=False, blockers=("Located in Germany",)),
            90,
            None,
            Decision.HIDE,
            "not_eligible",
        ),
        (RulesResult(eligible=True), 20, None, Decision.HIDE, "weak"),
        (RulesResult(eligible=True, scam_risk="medium"), 95, None, Decision.DIGEST, "possible_scam"),
        (RulesResult(eligible=True), 95, 0.3, Decision.DIGEST, "unsure"),
        (RulesResult(eligible=True), 75, 0.9, Decision.ALERT, "strong"),
        (RulesResult(eligible=True), 50, None, Decision.DIGEST, "fair"),
    ],
)
def test_decision_table(
    rules: RulesResult, score: int, confidence: float | None, decision: Decision, rule: str
) -> None:
    outcome = decide(rules, score, Thresholds(), confidence)
    assert (outcome.decision, outcome.rule) == (decision, rule)


async def test_strong_free_match_alerts_without_ai() -> None:
    result = await evaluate(make_job(), make_profile(), NOW)
    assert result.decision is Decision.ALERT
    assert not result.ai_used
    assert result.reasons


async def test_ineligible_jobs_never_reach_the_ai() -> None:
    ai = FakeAi(AiScore(99, 1.0))
    result = await evaluate(make_job(country_codes=["DE"]), make_profile(), NOW, ai=ai)
    assert (result.decision, result.score, ai.calls) == (Decision.HIDE, 0, 0)


async def test_weak_free_score_never_reaches_the_ai() -> None:
    ai = FakeAi(AiScore(99, 1.0))
    job = make_job(title="Sales Manager", description="Field sales.")
    result = await evaluate(job, make_profile(), NOW, ai=ai)
    assert (result.decision, ai.calls) == (Decision.HIDE, 0)


async def test_ai_score_blends_in_and_leads_the_reasons() -> None:
    ai = FakeAi(AiScore(20, 0.9, ("Needs Rust, which your resume does not show",)))
    result = await evaluate(make_job(), make_profile(), NOW, ai=ai)
    assert result.ai_used
    assert result.reasons[0] == "Needs Rust, which your resume does not show"
    assert result.decision is not Decision.ALERT


async def test_without_any_ai_the_free_score_decides() -> None:
    ai = FakeAi(AiScore(10, 1.0), available=False)
    result = await evaluate(make_job(), make_profile(), NOW, ai=ai)
    assert (result.ai_used, result.decision, ai.calls) == (False, Decision.ALERT, 0)


async def test_an_ai_that_cannot_answer_now_sends_the_job_to_the_digest() -> None:
    # A real session: the day's AI limit ran out and every free-score match became an instant alert.
    result = await evaluate(make_job(), make_profile(), NOW, ai=FakeAi(None))
    assert (result.ai_used, result.decision, result.outcome.rule) == (False, Decision.DIGEST, "unchecked")
    assert result.score >= Thresholds().alert_at  # the score is kept; only the interruption waits


@pytest.fixture
def matcher(job_store: JobStore, conn: Any, queue: TaskQueue, clock: FakeClock) -> Any:
    def build(profile: Profile | None) -> Matcher:
        return Matcher(job_store, MatchStore(conn, clock=clock), queue, lambda: profile, clock=clock)

    return build


async def test_handler_stores_the_match_and_queues_an_alert(matcher: Any, job_store: JobStore) -> None:
    saved = job_store.save(make_job())
    m: Matcher = matcher(make_profile())
    await m.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = m.matches.get(saved.job_id)
    assert record is not None
    assert record.decision == "alert"
    assert record.profile_version == match_version(make_profile())
    alert = m.queue.claim([ALERT_TASK])
    assert alert is not None
    assert alert.key == saved.job_id
    assert m.matches.counts() == {"alert": 1}


async def test_hidden_job_is_stored_with_its_reason_and_not_alerted(
    matcher: Any, job_store: JobStore
) -> None:
    saved = job_store.save(make_job(country_codes=["DE"]))
    m: Matcher = matcher(make_profile())
    await m.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = m.matches.get(saved.job_id)
    assert record is not None
    assert (record.decision, record.hidden_because) == ("hide", "Located in Germany, outside your countries")
    assert m.queue.claim([ALERT_TASK]) is None


async def test_handler_waits_for_a_profile_and_fails_for_missing_jobs(matcher: Any) -> None:
    with pytest.raises(DeferError):
        await matcher(None).handle(TaskRecord(1, "match", "any", {}, 1))
    with pytest.raises(PermanentError):
        await matcher(make_profile()).handle(TaskRecord(1, "match", "missing", {}, 1))


async def test_handler_judges_the_full_text_not_the_snippet(matcher: Any, job_store: JobStore) -> None:
    snippet_only = make_job(description="Build payment APIs.")
    saved = job_store.save(
        snippet_only, full_text="Build payment APIs. Requires 9+ years of backend experience."
    )
    m: Matcher = matcher(make_profile())
    await m.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = m.matches.get(saved.job_id)
    assert record is not None
    assert (record.decision, record.hidden_because) == ("hide", "Asks for 9+ years of experience; you have 3")


async def test_background_scoring_waits_while_the_user_is_chatting(
    job_store: JobStore, conn: Any, queue: TaskQueue, clock: FakeClock
) -> None:
    saved = job_store.save(make_job())
    busy = {"chat": True}
    matcher = Matcher(
        job_store,
        MatchStore(conn, clock=clock),
        queue,
        make_profile,
        ai=FakeAi(AiScore(80, 0.9)),
        ai_paused=lambda: busy["chat"],
        clock=clock,
    )
    with pytest.raises(DeferError):
        await matcher.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    busy["chat"] = False
    await matcher.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    assert matcher.matches.get(saved.job_id) is not None


async def test_ai_is_only_used_near_the_alert_line() -> None:
    ai = FakeAi(AiScore(90, 0.9))
    fair = make_job(  # free score between 35 and 50: a partial title match, a weak description, an old post
        title="Backend Platform Engineer",
        description="Kubernetes.",
        country_codes=["DE", "IN"],
        posted_at=NOW - timedelta(days=50),
    )
    result = await evaluate(fair, make_profile(), NOW, ai=ai)
    assert ai.calls == 0
    assert Thresholds().hide_below <= result.score < Thresholds().ai_from
