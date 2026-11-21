"""AI work for application documents: read the resume into sections, tailor it to a job, write a cover letter.

Patterns copied: ApplyPilot's tailoring (the header and every fact come from code, the model only words and
orders; banned clichés; one fresh retry with the problems listed), OpenApply's ``truthCheck`` (run by the
documents step afterwards) and Resume-Matcher's protected fields. The model never sees contact details: the
resume is scrubbed first, and the letter's greeting and signature are added by code.
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.privacy import scrub
from sarthi.ai.prompts import Prompt
from sarthi.profile.master import MasterResume

log = logging.getLogger(__name__)

RESUME_CHARS = 14_000
JOB_CHARS = 8_000

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]


# ---- reading the resume into sections -----------------------------------------------------------------

READ_PROMPT = Prompt(
    name="master_resume",
    version="1",
    instructions="""
Copy the resume into JSON sections. Copy text exactly as written: do not reword, summarise, translate or add
anything. Leave out contact details (they are removed anyway).

{"name": "", "headline": "", "summary": "",
 "experience": [{"company": "", "title": "", "location": "", "start": "", "end": "", "bullets": [""]}],
 "projects": [{"name": "", "description": "", "link": "", "bullets": [""]}],
 "education": [{"school": "", "degree": "", "start": "", "end": "", "details": [""]}],
 "skills": [{"label": "", "items": [""]}],
 "extras": [{"heading": "", "items": [""]}]}

Dates as written ("Jun 2023", "2021", "Present"). Skills: one item per tool or skill, grouped as the resume
groups them. Anything else (certifications, awards, languages, publications) goes in "extras".
""",
)


async def read_master(client: AiClient, resume_text: str) -> MasterResume | None:
    """The resume split into sections by the AI (not yet grounded), or None when no model answered."""
    if not client.available or not resume_text.strip():
        return None
    try:
        return await client.structured(
            READ_PROMPT, MasterResume, urgent=False, resume=scrub(resume_text[:RESUME_CHARS])
        )
    except AiError as error:
        log.info("resume not split into sections: %s", error)
        return None


# ---- tailoring to one job ---------------------------------------------------------------------------------


class TailoredJob(BaseModel):
    """New wording for one position's bullets, by position number."""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0, le=30)
    bullets: list[Text] = Field(default_factory=list, max_length=8)


class TailorAnswer(BaseModel):
    """What the model may change. Everything else is copied from the master resume by code."""

    model_config = ConfigDict(extra="ignore")

    headline: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=700)] = ""
    experience: list[TailoredJob] = Field(default_factory=list, max_length=20)
    project_order: list[int] = Field(default_factory=list, max_length=15)
    skills_first: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]] = Field(
        default_factory=list, max_length=25
    )


TAILOR_PROMPT = Prompt(
    name="tailor_resume",
    version="2",
    instructions="""
Tailor the candidate's resume to the job, the way a careful human editor would. You may only re-word and
re-order what <resume_sections> already says. Never add a skill, tool, employer, number, metric, team size or
result that is not in it.

Return JSON:
- "headline": a short line (max 8 words) naming the target role and 2-3 of the candidate's real skills,
  e.g. "Backend Engineer | Python, Node.js, RAG"
- "summary": at most 2 sentences and 45 words. Say what kind of engineer they are, the skills this job cares
  about most, and ONE standout result. Do not copy a bullet; the bullets already say the details. Each claim
  from one resume line, never combining details of different bullets. No "I", no "seeking", no buzzwords.
- "experience": for each position, {"index": <its number>, "bullets": [...]}: the bullets most relevant to
  the job first, re-worded to use the job's own words where the meaning is the same. Each bullet: starts with
  a plain past-tense verb (built, cut, wrote, moved, designed), one idea, about 25 words at most, the result
  last; keep every number exactly as written; at most 5 bullets per position; drop bullets unrelated to the
  job only if the position has more than 4.
- "project_order": project numbers, most relevant first (leave out unrelated ones)
- "skills_first": the candidate's skills that the job asks for, most important first (copy names exactly)

Write like a person, not a template: concrete nouns, short words, no filler ("various", "multiple",
"responsible for", "worked on" when "built" is true). No em dashes. Never use: passionate, leverage,
spearheaded, robust, cutting-edge, seamless, synergy, results-driven, detail-oriented, proven track record,
dynamic, innovative, utilize.
""",
)


def sections_text(master: MasterResume) -> str:
    """The master resume with numbered positions and projects, for prompts (no contact details)."""
    lines = [f"Headline: {master.headline}", f"Summary: {master.summary}", "", "Experience:"]
    for i, job in enumerate(master.experience):
        lines.append(f"[{i}] {job.title} at {job.company} ({job.start} - {job.end})")
        lines += [f"  - {b}" for b in job.bullets]
    lines.append("\nProjects:")
    for i, project in enumerate(master.projects):
        lines.append(f"[{i}] {project.name}: {project.description}")
        lines += [f"  - {b}" for b in project.bullets]
    lines.append("\nSkills: " + ", ".join(master.all_skills()))
    for school in master.education:
        lines.append(f"Education: {school.degree}, {school.school} ({school.start} - {school.end})")
    return "\n".join(lines)


async def tailor(
    client: AiClient, master: MasterResume, job_text: str, problems: list[str] | None = None
) -> TailorAnswer | None:
    """The model's tailoring answer, or None when no model answered. ``problems`` asks for a corrected try."""
    if not client.available:
        return None
    feedback = "\n".join(f"- {p}" for p in problems or []) or "none"
    try:
        return await client.structured(
            TAILOR_PROMPT,
            TailorAnswer,
            resume_sections=scrub(sections_text(master)),
            job_posting=job_text[:JOB_CHARS],
            problems_to_fix=feedback,
        )
    except AiError as error:
        log.info("resume not tailored: %s", error)
        return None


# ---- cover letter ---------------------------------------------------------------------------------------


class CoverAnswer(BaseModel):
    """The letter's body paragraphs (greeting and signature are added by code)."""

    model_config = ConfigDict(extra="ignore")

    paragraphs: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=900)]] = Field(
        min_length=2, max_length=4
    )


COVER_PROMPT = Prompt(
    name="cover_letter",
    version="2",
    instructions="""
Write the body of a short cover letter (3 paragraphs, 170-240 words in total) in the candidate's own voice,
to the company in <job_posting>, using only facts in <resume_sections>. It should read like a capable person
wrote it themselves for this one job: first person, warm but direct, sentences of different lengths, no
template phrases.

1. Name the role and open with the one thing from the resume that fits this job best, said plainly.
2. Pick one or two specific needs the job posting states and, for each, what the candidate built that
   answers it (from the resume). Keep every number exactly as written; add no new numbers, tools or results.
   Each claim must come from ONE resume line: never combine details of different bullets, projects or jobs,
   and never give the candidate a title they did not hold.
3. One or two sentences: what they would like to work on there (from the posting) and a plain request for a
   conversation.

No greeting, no signature, no subject line, no praise of the company ("industry leader", "renowned"). No em
dashes. Never use: passionate, thrilled, excited to apply, leverage, align with, demonstrate, resonate,
synergy, I believe, I am confident, furthermore, additionally, moreover, dynamic, innovative, esteemed,
I am writing to.
""",
)


async def cover_letter(
    client: AiClient, master: MasterResume, job_text: str, problems: list[str] | None = None
) -> CoverAnswer | None:
    """The letter body, or None when no model answered."""
    if not client.available:
        return None
    feedback = "\n".join(f"- {p}" for p in problems or []) or "none"
    try:
        return await client.structured(
            COVER_PROMPT,
            CoverAnswer,
            resume_sections=scrub(sections_text(master)),
            job_posting=job_text[:JOB_CHARS],
            problems_to_fix=feedback,
        )
    except AiError as error:
        log.info("cover letter not written: %s", error)
        return None


# ---- checking claims (an AI reviewer, after the code checks) ---------------------------------------------


class ClaimCheck(BaseModel):
    """Claims in a text that the resume does not support."""

    model_config = ConfigDict(extra="ignore")

    unsupported: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]] = Field(
        default_factory=list, max_length=10
    )


CLAIMS_PROMPT = Prompt(
    name="check_claims",
    version="1",
    instructions="""
You are a strict fact checker. <user_answer> was written about the candidate in <resume_sections>.
List every claim in it that is NOT directly supported by a single line of the resume: a detail moved from
one bullet, project or job to another, two separate facts combined into one, a job title the candidate did
not hold, or anything not in the resume at all. Opinions and the job's own requirements are not claims.

Return JSON: {"unsupported": ["<short quote of the claim> - <why>", ...]} ([] when everything is supported).
""",
)


async def unsupported_claims(client: AiClient, master: MasterResume, text: str) -> list[str] | None:
    """Claims in ``text`` the resume does not support; None when no model answered."""
    if not client.available or not text.strip():
        return [] if text.strip() == "" else None
    try:
        answer = await client.structured(
            CLAIMS_PROMPT, ClaimCheck, resume_sections=scrub(sections_text(master)), user_answer=text[:4000]
        )
    except AiError as error:
        log.info("claims not checked: %s", error)
        return None
    return answer.unsupported
