"""Putting the tailored resume and cover letter together from the master resume and the AI's suggestions.

Code owns every fact: name, contact, employers, titles, dates, schools and degrees are copied from the master
resume (Resume-Matcher's "protected fields"); the AI's text is used line by line only when it adds no new
number or skill and has no style problem; any line that fails falls back to the master's own words. With no
AI answer at all, the master resume is used as it is, so a document is always produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sarthi.ai.documents import CoverAnswer, TailorAnswer
from sarthi.documents.truth import fact_problems, style_problems
from sarthi.profile.master import Job, MasterResume, SkillGroup

MIN_BULLETS_KEPT = 2
MAX_BULLETS = 6


@dataclass
class Built:
    """A document and what had to be changed to keep it true."""

    resume: MasterResume
    reverted: list[str] = field(default_factory=list)  # AI lines replaced by the master's own words
    problems: list[str] = field(default_factory=list)  # style problems, to ask the AI again about


def _tidy(text: str) -> str:
    """AI text with its em dashes made plain and whitespace normalised."""
    text = re.sub(r"\s*—\s*", ", ", text)
    return re.sub(r"\s+", " ", text).strip()


class Checker:
    """Checks one AI line against the master resume."""

    def __init__(
        self, master: MasterResume, resume_text: str, own_skills: tuple[str, ...], *, strict: bool = False
    ) -> None:
        """Known facts are ``resume_text`` (the uploaded resume) and the person's skills.

        ``strict`` also replaces lines with style problems (used after the AI had its second try).
        """
        self.known_text = resume_text
        self.skills = (*master.all_skills(), *own_skills)
        self.strict = strict

    def facts(self, text: str) -> list[str]:
        """New numbers or skills in ``text``."""
        return fact_problems(text, self.known_text, self.skills)

    def style(self, text: str) -> list[str]:
        """Clichés, AI talk, em dashes."""
        return style_problems(text)


def _use(candidate: str, fallback: str, where: str, checker: Checker, built: Built) -> str:
    text = _tidy(candidate)
    if not text:
        return fallback
    facts = checker.facts(text)
    if facts:
        built.reverted.append(f"{where}: {'; '.join(facts)}")
        return fallback
    style = checker.style(text)
    if style and checker.strict:
        built.reverted.append(f"{where}: {'; '.join(style)}")
        return fallback
    built.problems += [f"{where} {p}" for p in style]
    return text


def _bullets(job: Job, suggested: list[str], where: str, checker: Checker, built: Built) -> tuple[str, ...]:
    kept: list[str] = []
    for i, bullet in enumerate(suggested[:MAX_BULLETS]):
        text = _use(bullet, "", f"{where} bullet {i + 1}", checker, built)
        if text and text not in kept:
            kept.append(text)
    if len(kept) < min(MIN_BULLETS_KEPT, len(job.bullets)):
        return job.bullets[:MAX_BULLETS]  # too much was untrue: the master's own bullets
    return tuple(kept)


def tailored_resume(
    master: MasterResume,
    answer: TailorAnswer | None,
    resume_text: str,
    own_skills: tuple[str, ...] = (),
    *,
    strict: bool = False,
) -> Built:
    """The resume for one job. Facts from ``master``; wording and order from ``answer`` where it is true."""
    built = Built(master)
    if answer is None:
        return built
    checker = Checker(master, resume_text, own_skills, strict=strict)
    by_index = {item.index: item.bullets for item in answer.experience}
    experience = tuple(
        job.model_copy(update={"bullets": _bullets(job, by_index[i], f"{job.company}", checker, built)})
        if by_index.get(i)
        else job
        for i, job in enumerate(master.experience)
    )
    order = [i for i in dict.fromkeys(answer.project_order) if 0 <= i < len(master.projects)]
    projects = tuple(master.projects[i] for i in order) if order else master.projects
    built.resume = master.model_copy(
        update={
            "headline": _use(answer.headline, master.headline, "headline", checker, built),
            "summary": _use(answer.summary, master.summary, "summary", checker, built),
            "experience": experience,
            "projects": projects,
            "skills": _skills_first(master, answer.skills_first),
        }
    )
    return built


def _skills_first(master: MasterResume, first: list[str]) -> tuple[SkillGroup, ...]:
    """The master's skill groups, with the job's skills (that the person has) moved to the front."""
    have = {s.lower(): s for s in master.all_skills()}
    wanted = [have[s.lower()] for s in first if s.lower() in have]
    if not wanted:
        return master.skills
    rank = {s: i for i, s in enumerate(wanted)}
    groups = []
    for group in master.skills:
        items = sorted(group.items, key=lambda s: rank.get(s, len(rank)))
        groups.append(group.model_copy(update={"items": tuple(items)}))
    groups.sort(key=lambda g: min((rank.get(s, len(rank)) for s in g.items), default=len(rank)))
    return tuple(groups)


# ---- cover letter ---------------------------------------------------------------------------------------


@dataclass
class Letter:
    """A cover letter: greeting, paragraphs, sign-off (greeting and sign-off are written by code)."""

    greeting: str
    paragraphs: list[str]
    sign_off: str
    from_ai: bool
    problems: list[str] = field(default_factory=list)

    def text(self) -> str:
        """The letter as plain text."""
        return "\n\n".join([self.greeting, *self.paragraphs, self.sign_off])


MIN_LETTER_PARAGRAPHS = 2


def _as_clause(text: str) -> str:
    """A resume line read on inside a sentence: 'Built an API.' becomes 'built an API'."""
    text = text.strip().rstrip(".")
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def plain_letter(master: MasterResume, role: str, company: str) -> list[str]:
    """A short, true letter built from the resume alone (used only when no AI letter passes the checks)."""
    latest = master.experience[0] if master.experience else None
    opening = f"I'd like to apply for the {role} role at {company}."
    if latest:
        opening += f" I'm currently a {latest.title} at {latest.company}"
        opening += f", where I {_as_clause(latest.bullets[0])}." if latest.bullets else "."
    skills = master.all_skills()[:4]
    middle = f"Most of my day-to-day work is in {_and_list(skills)}." if skills else ""
    if master.projects and master.projects[0].description:
        project = master.projects[0]
        middle += f" Outside work I built {project.name}, which {_lower_rest(project.description)}."
    closing = f"I'd be glad to talk about how I could help the team at {company}. Thank you for your time."
    return [p.strip() for p in (opening, middle, closing) if p.strip()]


def _lower_rest(description: str) -> str:
    """A project description read on after "which": "Built a tool that..." -> "is a tool that..."."""
    text = description.strip().rstrip(".")
    built = re.match(r"(?i)^(built|created|made|developed|designed)\s+(a|an|the)\s+", text)
    return f"is {built.group(2).lower()} {text[built.end() :]}" if built else _as_clause(text)


def cover_letter(  # noqa: PLR0913  (all the letter's inputs)
    master: MasterResume,
    answer: CoverAnswer | None,
    resume_text: str,
    *,
    role: str,
    company: str,
    own_skills: tuple[str, ...] = (),
    strict: bool = False,
) -> Letter:
    """The letter for one job.

    A first try must be wholly true; its problems go back to the AI for one more try. On that ``strict`` try
    the paragraphs that are true and cliché-free are kept when at least two are; only then is the plain letter
    built from the resume used. A plain letter carries the problems, so the caller can ask again.
    """
    sign_off = f"Best regards,\n{master.name}" if master.name else "Best regards,"
    greeting = f"Dear {company} hiring team,"
    plain = plain_letter(master, role, company)
    if answer is None:
        return Letter(greeting, plain, sign_off, from_ai=False)
    checker = Checker(master, resume_text, own_skills)
    paragraphs = [_tidy(p) for p in answer.paragraphs if _tidy(p)]
    checked = [(p, checker.facts(p), checker.style(p)) for p in paragraphs]
    facts = [f for _, found, _ in checked for f in found]
    style = [s for _, _, found in checked for s in found]
    if strict:
        kept = [p for p, bad_facts, bad_style in checked if not bad_facts and not bad_style]
        if len(kept) >= MIN_LETTER_PARAGRAPHS:
            return Letter(greeting, kept, sign_off, from_ai=True)
        return Letter(greeting, plain, sign_off, from_ai=False, problems=facts + style)
    if paragraphs and not facts:
        return Letter(greeting, paragraphs, sign_off, from_ai=True, problems=style)
    return Letter(greeting, plain, sign_off, from_ai=False, problems=facts + style)


def without_claims(letter: Letter, claims: list[str]) -> Letter | None:
    """The letter without the paragraphs the reviewer's claims quote.

    None when fewer than two paragraphs would remain, or when a claim cannot be found in any paragraph (it
    cannot be removed, so the letter cannot be trusted).
    """
    quotes = [c.split(" - ")[0].strip(" \"'“”").lower()[:40] for c in claims]
    if not all(q and any(q in p.lower() for p in letter.paragraphs) for q in quotes):
        return None
    kept = [p for p in letter.paragraphs if not any(q in p.lower() for q in quotes)]
    if len(kept) < MIN_LETTER_PARAGRAPHS:
        return None
    return Letter(letter.greeting, kept, letter.sign_off, from_ai=True)
