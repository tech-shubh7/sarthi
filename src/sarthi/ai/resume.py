"""Reading a resume with the AI: skills, work history and likely target roles.

The AI only *lists* facts (each job's title and dates); Sarthi computes the years of experience itself, so a
small local model cannot invent a total (a 3B model once read about 1 year of work as 6). Past intern and
trainee titles are not suggested as roles to search for. Contact details are removed before the text leaves
the machine, and the resume is fenced as data. Every value is only a suggestion the user confirms or edits.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.privacy import scrub
from sarthi.ai.prompts import Prompt

log = logging.getLogger(__name__)

RESUME_CHARS = 8000
MONTHS_PER_YEAR = 12
INTERNSHIP_WEIGHT = 0.5
_JUNIOR_TITLE = re.compile(r"\b(?:intern(?:ship)?|trainee|apprentice)\b", re.IGNORECASE)
_MONTH = re.compile(r"^(?P<year>(?:19|20)\d{2})(?:-(?P<month>0?[1-9]|1[0-2]))?$")
_PRESENT = frozenset({"present", "now", "current", "today", "ongoing"})

Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Stamp = Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)]


class WorkEntry(BaseModel):
    """One job on the resume."""

    model_config = ConfigDict(extra="ignore")

    title: Item
    start: Stamp = Field(description="YYYY-MM")
    end: Stamp = Field(default="present", description="YYYY-MM or present")
    internship: bool = False


class ResumeAnswer(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    skills: list[Item] = Field(default_factory=list, max_length=60)
    work: list[WorkEntry] = Field(default_factory=list, max_length=30)
    roles: list[Item] = Field(default_factory=list, max_length=8)


class ResumeFacts(BaseModel):
    """Suggestions for the setup conversation."""

    skills: list[str] = Field(default_factory=list)
    years_experience: float | None = None
    roles: list[str] = Field(default_factory=list)


RESUME_PROMPT = Prompt(
    name="resume_facts",
    version="2",
    instructions="""
Read the resume and return JSON with:
- "skills": up to 25 concrete skills, tools and technologies it shows (e.g. "Python", "PostgreSQL", "AWS"),
  most important first; no soft skills; no duplicates
- "work": every job or internship, each {"title", "start": "YYYY-MM", "end": "YYYY-MM" or "present",
  "internship": true/false}; copy the dates exactly as the resume gives them; do not include education
- "roles": up to 5 job titles this person should apply for NEXT, at their current level (not past internship
  or trainee titles)
""",
)


def _month_index(stamp: str, today: date) -> int | None:
    text = stamp.strip().lower()
    if text in _PRESENT:
        return today.year * MONTHS_PER_YEAR + today.month - 1
    match = _MONTH.match(text)
    if match is None:
        return None
    return int(match["year"]) * MONTHS_PER_YEAR + int(match["month"] or 1) - 1


def years_of_experience(work: list[WorkEntry], today: date) -> float | None:
    """Total years from dated jobs: overlapping months count once, internship months count half."""
    months: dict[int, float] = {}
    for entry in work:
        start, end = _month_index(entry.start, today), _month_index(entry.end, today)
        if start is None or end is None or end < start:
            continue
        weight = INTERNSHIP_WEIGHT if entry.internship or _JUNIOR_TITLE.search(entry.title) else 1.0
        for month in range(start, min(end, _month_index("present", today) or end) + 1):
            months[month] = max(months.get(month, 0.0), weight)
    if not months:
        return None
    return round(sum(months.values()) / MONTHS_PER_YEAR, 1)


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for item in items:
        seen.setdefault(item.lower(), item)
    return list(seen.values())


def facts_from(answer: ResumeAnswer, today: date) -> ResumeFacts:
    """Suggestions computed from the model's answer."""
    years = years_of_experience(answer.work, today)
    roles = _unique(answer.roles)
    if years is None or years >= 1:  # past intern/trainee titles are not next steps for experienced people
        roles = [role for role in roles if not _JUNIOR_TITLE.search(role)]
    return ResumeFacts(skills=_unique(answer.skills)[:25], years_experience=years, roles=roles[:5])


async def read_resume(client: AiClient, resume_text: str, today: date | None = None) -> ResumeFacts | None:
    """Suggestions from the resume, or None when no AI model is available."""
    if not client.available or not resume_text.strip():
        return None
    try:
        answer = await client.structured(
            RESUME_PROMPT, ResumeAnswer, urgent=True, resume=scrub(resume_text[:RESUME_CHARS])
        )
    except AiError as error:
        log.info("resume not read by AI: %s", error)
        return None
    return facts_from(answer, today or date.today())  # noqa: DTZ011  (the user's local calendar date)
