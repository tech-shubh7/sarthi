"""Layer 2 of the match funnel: the AI reads the full job and the user's profile.

Design follows JobRadar (MIT): the model answers typed questions, each 0-1, and code combines them into
the score with fixed weights; the model's own "overall" is one input, not the verdict. Hard eligibility
(country, visa, job type) stays with the rules layer, which runs before this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.privacy import scrub
from sarthi.ai.prompts import Prompt
from sarthi.jobs.models import JobPosting
from sarthi.jobs.requirements import too_experienced_for
from sarthi.profile.models import Profile

log = logging.getLogger(__name__)

RESUME_CHARS = 6000
JOB_CHARS = 8000
NOT_ELIGIBLE_BELOW = 0.5
NOT_ELIGIBLE_FACTOR = 0.3
LEVEL_MISMATCH_BELOW = 0.35  # the AI says the job's level is far from the candidate's experience
SURE_ENOUGH = 0.6  # a level judgement from a vague or empty posting is not used to hide a job
_WEIGHTS = {"role_fit": 0.30, "skills_fit": 0.35, "seniority_fit": 0.15, "overall_fit": 0.20}

Unit = Annotated[float, Field(ge=0, le=1)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=160)]


@dataclass(frozen=True, slots=True)
class AiScore:
    """The AI layer's verdict: 0-100 fit, how sure it is (0-1), and its reasons."""

    score: int
    confidence: float
    reasons: tuple[str, ...] = ()
    blocker: str | None = None  # why the job is out of reach, judged from the full posting


class AiScorer(Protocol):
    """Layer 2. Returns None when it cannot answer (no model set up, budget spent, provider down)."""

    @property
    def available(self) -> bool:
        """True when a model is set up (it may still fail to answer right now)."""
        ...

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        """Judge how well ``job`` fits ``profile``."""
        ...


class MatchAnswers(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    eligible: Unit = Field(
        description="Could the candidate realistically be hired, from stated requirements?"
    )
    role_fit: Unit
    skills_fit: Unit
    seniority_fit: Unit
    overall_fit: Unit
    confidence: Unit
    min_years_required: float | None = Field(default=None, ge=0, le=40)
    reasons: list[Reason] = Field(default_factory=list, max_length=3)


MATCH_PROMPT = Prompt(
    name="match",
    version="2",
    instructions="""
Judge how well the job fits the candidate. Use only what the job posting and candidate text say.

Return JSON with these keys, each a number from 0 to 1 unless stated:
- "eligible": could they realistically be hired, judging by required degrees, years and must-have skills
- "role_fit": is this the kind of role they want (see preferences)
- "skills_fit": share of the job's must-have skills the resume shows
- "seniority_fit": does the job's level match their experience (a senior, "III", staff or lead role for
  someone with 1-2 years is 0.1 or lower)
- "overall_fit": your overall judgement
- "confidence": how sure you are (low when the posting is vague or very short)
- "min_years_required": the minimum years of work experience the posting requires (a number, e.g. 5 for
  "5-8 years"), or null when it states none
- "reasons": up to 3 short sentences for the candidate, most important first; name concrete matching or
  missing skills. Do not mention location, visa or salary.
""",
)


def _candidate_text(profile: Profile) -> str:
    prefs = profile.preferences
    years = f"{prefs.years_experience:g}" if prefs.years_experience is not None else "not stated"
    return (
        f"Target roles: {', '.join(prefs.roles)}\n"
        f"Skills: {', '.join((*profile.skills, *prefs.keywords)) or 'not stated'}\n"
        f"Years of experience: {years}"
    )


def _preferences_text(profile: Profile) -> str:
    prefs = profile.preferences
    parts = [
        f"Looking for: {prefs.looking_for}" if prefs.looking_for else "",
        f"Rules out: {prefs.ruled_out}" if prefs.ruled_out else "",
    ]
    return "\n".join(p for p in parts if p) or "Nothing beyond the target roles."


def combine(answers: MatchAnswers) -> int:
    """The 0-100 score from the model's answers; a probably-ineligible job is scaled down hard."""
    raw = sum(float(getattr(answers, name)) * weight for name, weight in _WEIGHTS.items())
    if answers.eligible < NOT_ELIGIBLE_BELOW:
        raw *= NOT_ELIGIBLE_FACTOR
    return max(0, min(100, round(raw * 100)))


def blocker_for(answers: MatchAnswers, years: float | None) -> str | None:
    """Why the posting is out of reach for someone with ``years`` of experience, from the AI's reading."""
    if years is None:
        return None
    needed = answers.min_years_required
    if needed is not None and too_experienced_for(needed, years):
        return f"Asks for {needed:g}+ years of experience; you have {years:g}"
    if answers.seniority_fit < LEVEL_MISMATCH_BELOW and answers.confidence >= SURE_ENOUGH:
        return f"Level is well above your {years:g} year(s) of experience"
    return None


class LlmMatchScorer:
    """Scores jobs with the configured AI models."""

    def __init__(self, client: AiClient) -> None:
        """Ask ``client``'s models."""
        self._client = client

    @property
    def available(self) -> bool:
        """True when the client has a model to ask."""
        return self._client.available

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        """The AI's verdict, or None when no model can answer right now."""
        if not self._client.available:
            return None
        posting = f"Title: {job.title}\nCompany: {job.company}\n\n{job.description[:JOB_CHARS]}"
        try:
            answers = await self._client.structured(
                MATCH_PROMPT,
                MatchAnswers,
                job_posting=posting,
                candidate=_candidate_text(profile),
                preferences=_preferences_text(profile),
                resume=scrub(profile.resume_text[:RESUME_CHARS]) or "No resume text.",
            )
        except AiError as error:
            log.info("AI score skipped for %s: %s", job.url, error)
            return None
        blocker = blocker_for(answers, profile.preferences.years_experience)
        return AiScore(combine(answers), answers.confidence, tuple(answers.reasons), blocker)
