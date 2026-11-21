"""The match funnel, cheapest layer first (docs/ARCHITECTURE.md §2.7).

L1 hard rules -> L0 free score -> L2 AI score (only for jobs still in the running) -> decision.
A job that fails a rule or scores too low on the free layers never costs an AI call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sarthi.ai.scoring import AiScore, AiScorer
from sarthi.jobs.models import JobPosting
from sarthi.matching.decide import Decision, Outcome, Thresholds, decide
from sarthi.matching.prefilter import prefilter
from sarthi.matching.rules import check_rules
from sarthi.profile.models import Profile

AI_WEIGHT = 0.6
# A job is worth a look only if its title is one of the user's roles, or it asks for several of their skills.
# Without this, location and recency points alone lift unrelated jobs ("Java Developer") into the digest.
MIN_SKILLS_WITHOUT_TITLE = 4  # the AI's read of the full job counts more than keyword overlap
MAX_REASONS = 4


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Everything the next steps need: score, decision, and the lines shown to the user."""

    score: int
    outcome: Outcome
    reasons: tuple[str, ...]
    notes: tuple[str, ...]
    ai_used: bool

    @property
    def decision(self) -> Decision:
        """Shortcut for ``outcome.decision``."""
        return self.outcome.decision


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


async def evaluate(
    job: JobPosting,
    profile: Profile,
    now: datetime,
    thresholds: Thresholds = Thresholds(),  # noqa: B008  (frozen dataclass, safe default)
    ai: AiScorer | None = None,
) -> MatchResult:
    """Run the funnel for one job."""
    rules = check_rules(job, profile)
    if not rules.eligible:
        outcome = decide(rules, 0, thresholds)
        return MatchResult(0, outcome, (), rules.notes, ai_used=False)

    free = prefilter(job, profile, now)
    if free.title_share == 0 and free.skill_hits < MIN_SKILLS_WITHOUT_TITLE:
        why = "Not one of your target roles, and it asks for few of your skills"
        return MatchResult(0, Outcome(Decision.HIDE, "not_relevant", why), free.reasons, rules.notes, False)
    free_score = _clamp(free.score + rules.location_points)
    reasons = free.reasons
    ai_verdict: AiScore | None = None
    ai_wanted = ai is not None and ai.available and free_score >= thresholds.ai_from
    if ai is not None and ai_wanted:
        ai_verdict = await ai.score(job, profile)

    if ai_verdict is not None and ai_verdict.blocker is not None:
        outcome = Outcome(Decision.HIDE, "not_eligible", ai_verdict.blocker)
        return MatchResult(0, outcome, ai_verdict.reasons[:MAX_REASONS], rules.notes, ai_used=True)

    if ai_verdict is None:
        score, confidence = free_score, None
    else:
        score = _clamp((1 - AI_WEIGHT) * free_score + AI_WEIGHT * ai_verdict.score)
        confidence = ai_verdict.confidence
        reasons = ai_verdict.reasons + reasons

    outcome = decide(rules, score, thresholds, confidence)
    if outcome.decision is Decision.ALERT and free.title_share == 0:
        outcome = Outcome(Decision.DIGEST, "skills_only")  # instant alerts are for the user's own roles
    elif outcome.decision is Decision.ALERT and ai_wanted and ai_verdict is None:
        # The AI is set up but could not read the full posting now (limit reached, provider down): years
        # asked, level and must-have skills are unchecked, so the job waits for the digest, not the phone.
        outcome = Outcome(Decision.DIGEST, "unchecked")
    return MatchResult(score, outcome, reasons[:MAX_REASONS], rules.notes, ai_used=ai_verdict is not None)
