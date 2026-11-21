# Label reading ported from JobRadar (MIT), src/jobradar/pipeline/post_labels.py and values.py; page diffing
# from src/jobradar/sources/pages/text.py @ fdd739b. Copyright (c) ThePlator.
# Modifications (c) Sarthi contributors: output is Sarthi's JobPosting; an AI reads only posts the rules
# could not (fast model, fixed JSON shape).
"""Jobs from free text: Telegram channel posts and new lines on watched hiring pages.

Most channels write "Company: X / Role: Y / Location: Z" lines or "X is hiring Y": read free, no AI.
Posts the rules cannot read go to the AI, which must say whether the post is a job at all. A post needs an
apply link (or the page's own address) to become a job; a post without a company and title is dropped.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.prompts import Prompt
from sarthi.jobs.models import JobPosting

log = logging.getLogger(__name__)

POST_CHARS = 4000

# ---- labelled lines ---------------------------------------------------------------------------------------

_LABELS: dict[str, tuple[str, ...]] = {
    "company": ("company", "company name", "organization", "organisation", "organization name",
                "organisation name", "hiring company", "employer"),
    "role": ("role", "job role", "post", "post name", "position", "position name", "job title", "designation",
             "profile", "job profile"),
    "location": ("location", "locations", "job location", "work location", "place of posting"),
    "experience": ("experience", "exp", "experience required", "work experience"),
    "salary": ("salary", "expected salary", "ctc", "stipend", "package", "compensation"),
}  # fmt: skip
_KIND = {label: kind for kind, labels in _LABELS.items() for label in labels}
_LINE = re.compile(
    r"^(?P<label>"
    + "|".join(re.escape(label) for label in sorted(_KIND, key=len, reverse=True))
    + r")(?:\s*:|\s+[-–—])\s*(?P<value>.+)$",
    re.IGNORECASE,
)
_NOT_A_ROLE = re.compile(
    r"^(in|at|for|from|across|with|on|now|immediately|freshers|graduates|candidates|students|multiple|various)\b",
    re.IGNORECASE,
)
_HIRING = re.compile(r"^(?P<company>.+?)\s+is\s+hiring\s+(?:for\s+)?(?:an?\s+)?(?P<role>.+)$", re.I)
_PLACEHOLDER_ROLES = re.compile(r"^(multiple|various|many|several)\s+(roles|positions|openings)\b", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_ATS_HOSTS = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|"
                        r"workable\.com|darwinbox\.in|keka\.com|icims\.com|forms\.gle|docs\.google\.com/forms")  # fmt: skip


def _is_emoji(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category in ("So", "Cf") or ch == "️" or (category == "Sk" and ord(ch) > 127)  # noqa: PLR2004


def plain_line(line: str) -> str:
    """NFKC (decorative bold letters become plain), emoji and leading bullets dropped."""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", line) if not _is_emoji(ch))
    return " ".join(re.sub(r"^[\W_]+", "", text).split())


def clean_company(value: str) -> str | None:
    """'HARMAN (a Samsung company)' -> 'HARMAN'."""
    text = " ".join(re.sub(r"\([^)]*\)", " ", value).split()).strip(" .-*|")
    return text if 1 < len(text) <= 80 else None  # noqa: PLR2004


def clean_role(value: str) -> str | None:
    """'Software Intern | R-56145-2026' -> 'Software Intern'; placeholders ("Multiple roles") are None."""
    text = " ".join(re.sub(r"\s+\|[^|]*\d.*$", "", value).split()).strip(" .-*|")
    if _PLACEHOLDER_ROLES.match(text):
        return None
    return text if 1 < len(text) <= 120 else None  # noqa: PLR2004


def labels(text: str) -> dict[str, str]:
    """{kind: value} for the first line of each kind, e.g. {"company": "Wipro"}."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if (
            (m := _LINE.match(plain_line(line)))
            and (value := m["value"].strip())
            and not value.lower().startswith("http")
        ):
            found.setdefault(_KIND[m["label"].lower()], value)
    return found


def _hiring_line(text: str) -> dict[str, str]:
    lines = [p for line in text.splitlines() if (p := plain_line(line))]
    for line in lines[:3]:
        if (m := _HIRING.match(line)) and "http" not in line.lower():
            company, role = clean_company(m["company"]), clean_role(m["role"].rstrip("!. "))
            if company and role and len(company) <= 50 and not _NOT_A_ROLE.match(role):  # noqa: PLR2004
                return {"company": company, "role": role}
    return {}


def links(text: str) -> list[str]:
    """Web links in a post, apply forms and hiring systems first."""
    found = list(dict.fromkeys(u.rstrip(".,;:!") for u in _URL.findall(text)))
    return sorted(found, key=lambda u: 0 if _ATS_HOSTS.search(u) else 1)


@dataclass(frozen=True, slots=True)
class PostFields:
    """What a post says about the job."""

    company: str = ""
    title: str = ""
    location: str = ""
    experience: str = ""


def rule_fields(text: str) -> PostFields:
    """Fields from labelled lines and "X is hiring Y"."""
    found = labels(text)
    company = clean_company(found.get("company", "")) or ""
    title = clean_role(found.get("role", "")) or ""
    hiring = _hiring_line(text)
    return PostFields(
        company=company or hiring.get("company", ""),
        title=title or hiring.get("role", ""),
        location=found.get("location", "")[:200],
        experience=found.get("experience", "")[:100],
    )


# ---- the AI, for posts the rules cannot read --------------------------------------------------------------

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]


class PostAnswer(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    is_job: bool
    company: Short = ""
    title: Short = ""
    location: Short = ""


POST_PROMPT = Prompt(
    name="job_post",
    version="1",
    instructions="""
<job_posting> is a message from a job channel or a hiring page. Is it one job opening (not an ad for a course,
a list of many links, a webinar, or news)? If so, give the employer's name, the job title and the location as
written. Return JSON: {"is_job": true/false, "company": "", "title": "", "location": ""}.
""",
)


async def ai_fields(client: AiClient | None, text: str) -> PostFields | None:
    """The AI's reading of a post (None: not a job, or no model answered)."""
    if client is None or not client.available:
        return None
    try:
        answer = await client.structured(POST_PROMPT, PostAnswer, job_posting=text[:POST_CHARS])
    except AiError as error:
        log.info("post not read by AI: %s", error)
        return None
    if not answer.is_job:
        return None
    return PostFields(answer.company, answer.title, answer.location)


async def post_to_job(  # noqa: PLR0913  (a post and where it came from)
    text: str,
    *,
    source: str,
    posted_at: datetime | None,
    fallback_url: str = "",
    ai: AiClient | None = None,
    post_id: str | None = None,
) -> JobPosting | None:
    """The job a post announces, or None (not a job, no link, or no company and title)."""
    found = links(text)
    url = found[0] if found else fallback_url
    if not url:
        return None
    fields = rule_fields(text)
    if not (fields.company and fields.title):
        read = await ai_fields(ai, text)
        if read is None:
            return None
        fields = PostFields(
            fields.company or read.company, fields.title or read.title, fields.location or read.location
        )
    data: dict[str, Any] = {
        "source": source,
        "source_job_id": post_id,
        "url": url,
        "title": fields.title,
        "company": fields.company,
        "location_text": fields.location,
        "description": text[:POST_CHARS],
        "posted_at": posted_at,
    }
    try:
        return JobPosting.model_validate(data)
    except ValidationError:
        return None


# ---- what is new on a watched page ------------------------------------------------------------------------

MAX_PAGE_TEXT = 50_000
MIN_ADDED = 80  # fewer new characters than this is not an announcement
REDESIGN_SHARE = 0.6
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_RELATIVE = re.compile(
    r"\b(?:posted|updated|published|added)?\s*(?:\d+|a|an|few)\s+(?:second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago\b"
    r"|\b(?:posted|updated|published|added)\s+(?:today|yesterday|just now)\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE)
_COUNTER = re.compile(
    r"\b\d[\d,]*\+?\s+(?:jobs?|openings?|results?|positions?|vacanc(?:y|ies)|views?|applicants?|applications?|visitors?)\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    rf"\b\d{{1,4}}[-/.]\d{{1,2}}[-/.]\d{{1,4}}\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?(?:\s+\d{{4}})?"
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?|\b{_MONTH}\s+\d{{4}}\b",
    re.IGNORECASE,
)
_META = re.compile(r"^\W*(?:last\s+)?(?:updated|modified|posted|published|date|copyright|©|\(c\))\b", re.I)
_MIN_LINE_LETTERS = 12


def page_lines(text: str) -> list[str]:
    """Non-empty lines with spaces collapsed, capped in total size."""
    lines, size = [], 0
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        size += len(line) + 1
        if size > MAX_PAGE_TEXT:
            break
        lines.append(line)
    return lines


def line_key(line: str) -> str:
    """What a line says without timestamps and counters; "" for page furniture."""
    if _META.match(line):
        return ""
    key = _COUNTER.sub(" ", _CLOCK.sub(" ", _RELATIVE.sub(" ", line)))
    if len(re.sub(r"[^a-z]", "", _DATE.sub(" ", key).casefold())) < _MIN_LINE_LETTERS:
        return ""
    return " ".join(re.sub(r"[^\w\s]", " ", key.casefold()).split())


def added_lines(old: list[str], new: list[str]) -> list[str]:
    """Lines of ``new`` that say something no line of ``old`` said (a moved line is not new)."""
    before = {line_key(line) for line in old}
    added, seen = [], set()
    for line in new:
        key = line_key(line)
        if key and key not in before and key not in seen:
            seen.add(key)
            added.append(line)
    return added


def is_redesign(new: list[str], added: list[str]) -> bool:
    """Most of the page changed at once: a new layout, not an announcement."""
    total = sum(len(line) for line in new)
    return total > 0 and sum(len(line) for line in added) > REDESIGN_SHARE * total


def absolute_links(lines: list[str], page_url: str) -> list[str]:
    """Web links in the lines, absolute, without the page itself."""
    found: list[str] = []
    for line in lines:
        for href in _URL.findall(line):
            url = urljoin(page_url, href)
            if urlsplit(url).scheme in ("http", "https") and url.split("#")[0].rstrip("/") != page_url.rstrip(
                "/"
            ):
                found.append(url)
    return list(dict.fromkeys(found))[:10]
