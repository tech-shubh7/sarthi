# Fact checks ported from OpenApply (MIT), src/truth.ts (truthCheck) @ db7796e. Copyright (c) OpenApply
# contributors. Style lists from ApplyPilot (AGPL-3.0), scoring/validator.py (BANNED_WORDS, LLM_LEAK_PHRASES).
# Modifications (c) Sarthi contributors: Python; works on single texts so each tailored line can be checked
# and replaced on its own.
"""Nothing in a tailored document may be new: numbers, skills, employers and phrases are checked in code.

A number is "known" only when the person's own resume states it ("$300K+" and "300k" are the same claim);
numbers from the job posting are not the person's facts. A skill is known when the resume or the person's
skills name it. Style problems (clichés, an AI
talking about itself, em dashes) are reported separately so they can be fixed by asking again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sarthi.jobs.skills import skills_in

_FIGURE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|[kmb]\b|x\b)?\+?", re.I)

BANNED_PHRASES: tuple[str, ...] = (
    "passionate", "committed to", "utilizing", "utilize", "harnessing", "spearheaded", "spearhead",
    "orchestrated", "championed", "pioneered", "robust", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability", "team player", "fast learner",
    "self-starter", "go-getter", "synergy", "holistic", "transformative", "innovative solutions", "paradigm",
    "detail-oriented", "highly motivated", "results-driven", "seamless", "deep understanding",
    "extensive experience", "thrives in", "excels at", "adept at", "well-versed in", "i am confident",
    "i believe", "i am excited", "excited to apply", "thrilled", "instrumental in", "eager to", "leverage",
    "leveraging", "leveraged", "furthermore", "additionally", "moreover", "this demonstrates",
)  # fmt: skip
_BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("align with", r"\balign(?:s|ed|ing)?\s+with\b"),
    ("demonstrate", r"\bdemonstrat\w*\b"),
    ("resonate", r"\bresonat\w*\b"),
)
LEAK_PHRASES: tuple[str, ...] = (
    "i am sorry", "i apologize", "here is the", "here's the", "below is the",
    "as requested", "i have rewritten", "i have updated", "i have removed", "note:", "disclaimer:",
    "tailored resume", "[company]", "[company name]", "[your name]", "[name]", "{company}", "lorem ipsum",
)  # fmt: skip
# An AI talking about itself ("As an AI language model, I..."), not an AI engineer: "as an AI Engineer" and
# "built apps on large language models" are what an AI engineer's own resume says.
_AI_SELF = re.compile(
    r"\b(?:as an ai|i am an ai|i'm an ai|as an? (?:large )?language model|i am a (?:large )?language model)\b"
    r"(?!\s*(?:/|-|&|and\b)?\s*(?:engineer|developer|scientist|researcher|specialist|architect|intern|lead|"
    r"product|consultant|practitioner|ml\b|team|role|position))"
)


def figure_key(figure: str) -> str:
    """The comparable form of a number: "$300K+" -> "300k"."""
    return re.sub(r"[\s,$+]", "", figure.lower())


def figures(text: str) -> list[str]:
    """Every number-like claim in ``text`` as comparable keys."""
    return [k for k in (figure_key(m) for m in _FIGURE.findall(text or "")) if k]


def new_numbers(text: str, known_text: str) -> list[str]:
    """Numbers in ``text`` that ``known_text`` never states (the written form, for messages)."""
    known = set(figures(known_text))
    return [raw.strip() for raw in _FIGURE.findall(text or "") if figure_key(raw) not in known]


def new_skills(text: str, known_skills: Iterable[str], known_text: str) -> list[str]:
    """Skills ``text`` names that neither the person's skills nor ``known_text`` mention."""
    known = list(known_skills)
    already = set(skills_in(known_text, known))
    return [skill for skill in skills_in(text, known) if skill not in already]


def style_problems(text: str) -> list[str]:
    """Clichés, an AI talking about itself, placeholders and em dashes."""
    lower = (text or "").lower()
    problems = [f'uses "{p}"' for p in BANNED_PHRASES if re.search(rf"\b{re.escape(p)}\b", lower)]
    problems += [f'uses "{label}"' for label, pattern in _BANNED_PATTERNS if re.search(pattern, lower)]
    problems += [f'contains "{p}"' for p in LEAK_PHRASES if p in lower]
    if found := _AI_SELF.search(lower):
        problems.append(f'contains "{found.group(0)}"')
    if "—" in (text or ""):
        problems.append("uses an em dash")
    return problems


def fact_problems(text: str, known_text: str, known_skills: Iterable[str]) -> list[str]:
    """New numbers or skills in ``text``, compared with what the person's resume (and the job) say."""
    problems = [f'new number "{n}"' for n in new_numbers(text, known_text)]
    problems += [f'new skill "{s}"' for s in new_skills(text, known_skills, known_text)]
    return problems
