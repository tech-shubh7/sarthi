"""Layer 1 of the match funnel: hard yes/no rules, decided in code before any AI call.

Unknown facts never hide a job: they pass with a note ("visa sponsorship not mentioned"), so the user, or the
AI layer, can judge. Only facts the job states can rule it out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sarthi.jobs.geo import country_name, seniority_of
from sarthi.jobs.models import EmploymentType, JobPosting, RemoteType, Sponsorship
from sarthi.jobs.requirements import LEVEL_MIN_YEARS, min_years_in_title, min_years_required, years_tolerance
from sarthi.jobs.scam import check as scam_check
from sarthi.jobs.stack import missing_stack
from sarthi.profile.models import Profile, RemotePreference

LOCATION_FIT_POINTS = 15
LOCATION_UNSURE_POINTS = 9


@dataclass(frozen=True, slots=True)
class RulesResult:
    """Whether the job can be for this user, location points, and notes for the message."""

    eligible: bool
    location_points: int = 0
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    scam_risk: str = "low"


@dataclass
class _Verdict:
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    location_points: int = 0


def _names(codes: frozenset[str] | set[str]) -> str:
    return ", ".join(sorted(country_name(c) for c in codes))


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text) is not None


def _check_remote(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    if prefs.remote is RemotePreference.NO:
        verdict.blockers.append("Remote only, and you want on-site or hybrid work")
        return
    if job.is_remote_anywhere:
        verdict.location_points = LOCATION_FIT_POINTS
        verdict.notes.append("Remote, open worldwide")
    elif prefs.home_country in job.remote_countries:
        verdict.location_points = LOCATION_FIT_POINTS
        verdict.notes.append(f"Remote, open to people in {country_name(prefs.home_country)}")
    elif not job.remote_countries:  # a restriction Sarthi cannot place: a time zone, an unknown city
        verdict.location_points = LOCATION_UNSURE_POINTS
        verdict.notes.append(f"Remote, but limited to “{job.location_text[:80]}”: check it includes you")
    else:
        verdict.blockers.append(f"Remote only for people in {_names(job.remote_countries)}")


def _check_on_site(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    if prefs.remote is RemotePreference.ONLY and job.remote_type in (RemoteType.ONSITE, RemoteType.HYBRID):
        verdict.blockers.append(f"{job.remote_type.value.capitalize()} job, and you want remote only")
        return
    if not job.country_codes:
        verdict.location_points = LOCATION_UNSURE_POINTS
        verdict.notes.append("Location country not stated")
        return
    workable = job.country_codes & prefs.work_countries
    if not workable:
        verdict.blockers.append(f"Located in {_names(job.country_codes)}, outside your countries")
        return
    verdict.location_points = LOCATION_FIT_POINTS
    if workable & prefs.visa_free_countries:
        return
    if job.visa_sponsorship is Sponsorship.NO:
        verdict.blockers.append(f"No visa sponsorship, and you need one for {_names(workable)}")
    elif job.visa_sponsorship is Sponsorship.YES:
        verdict.notes.append("Offers visa sponsorship")
    else:
        verdict.notes.append(f"Visa sponsorship not mentioned (you need one for {_names(workable)})")


_INTERN_TITLE = re.compile(r"\bintern(?:ship)?s?\b|\btrainee\b|\bapprentice", re.I)


def _employment_type(job: JobPosting) -> EmploymentType:
    """The kind of contract; an intern title wins over what the source said ("MERN Developer Intern")."""
    return EmploymentType.INTERNSHIP if _INTERN_TITLE.search(job.title) else job.employment_type


# Job families that are not engineering, read from the title. A target role naming the family ("Sales
# Engineer", "Support Engineer") keeps such jobs.
_OTHER_FAMILIES: dict[str, str] = {
    "sales": r"\bsales\b|\bbusiness development\b|\bpre-?sales\b",
    "marketing": r"\bmarketing\b|\bsocial media\b|\bseo\b|\bcontent writer\b",
    "recruiting": r"\brecruit(?:er|ing|ment)\b|\btalent acquisition\b|\bhr\b|\bhuman resources\b",
    "support": r"\b(?:production|application|desktop|technical|it|customer|l[123])\s+support\b"
    r"|\bhelp\s*desk\b|\bsupport (?:engineer|analyst|specialist|executive)\b",
    "teaching": r"\btrainer\b|\bteacher\b|\btutor\b|\bfaculty\b|\blecturer\b",
    "project management": r"\bscrum master\b|\bproject manager\b|\bprogram manager\b|\bdelivery manager\b",
    "operations": r"\bfield service\b|\btechnician\b|\bback office\b|\bdata entry\b|\bcall cent(?:er|re)\b",
}


_MANAGES = re.compile(r"\b(?:manager|head|director|lead|vp)\b", re.I)


def _check_family(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    wanted = " | ".join(profile.preferences.roles).lower()
    for family, pattern in _OTHER_FAMILIES.items():
        if re.search(pattern, job.title, re.I) and not re.search(pattern, wanted, re.I):
            verdict.blockers.append(f"Looks like a {family} job, not one of your target roles")
            return


def _check_stack(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    missing = missing_stack(job.title, (*profile.skills, *profile.preferences.keywords))
    if missing:
        verdict.blockers.append(f"Built around {', '.join(sorted(missing))}, which is not in your skills")


def _check_job_terms(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    kind = _employment_type(job)
    if kind is not EmploymentType.UNKNOWN and kind not in prefs.employment_types:
        verdict.blockers.append(f"{kind.value.replace('_', '-').capitalize()} role")
    years = prefs.years_experience
    if years is None:
        return
    level = seniority_of(job.title)
    if level == "manager" and any(_MANAGES.search(role) for role in prefs.roles):
        level = None  # the user wants manager roles: "manager" names the job, not a level above them
    level_years = LEVEL_MIN_YEARS.get(level or "")
    if level_years is not None and level_years > years + years_tolerance(years):
        verdict.blockers.append(
            f"{level.capitalize() if level else ''} level role; you have {years:g} year(s)"
        )
        return
    needed = min_years_in_title(job.title) or min_years_required(job.description)
    if needed is not None:
        if needed > years + years_tolerance(years):
            verdict.blockers.append(f"Asks for {needed}+ years of experience; you have {years:g}")
        elif needed > years:
            verdict.notes.append(f"Asks for {needed}+ years; you have {years:g}")


def _check_exclusions(job: JobPosting, profile: Profile, verdict: _Verdict) -> None:
    prefs = profile.preferences
    company = job.company.lower()
    for excluded in prefs.exclude_companies:
        if excluded.lower() in company:
            verdict.blockers.append(f"Excluded company: {excluded}")
            return
    text = f"{job.title}\n{job.description}".lower()
    for word in prefs.exclude_keywords:
        if _contains_word(text, word):
            verdict.blockers.append(f"Contains excluded keyword: {word}")
            return


def check_rules(job: JobPosting, profile: Profile) -> RulesResult:
    """Apply every hard rule to ``job``."""
    verdict = _Verdict()
    _check_exclusions(job, profile, verdict)
    if job.remote_type is RemoteType.REMOTE:
        _check_remote(job, profile, verdict)
    else:
        _check_on_site(job, profile, verdict)
    _check_job_terms(job, profile, verdict)
    _check_stack(job, profile, verdict)
    _check_family(job, profile, verdict)
    scam = scam_check(job.description)
    if scam.risk == "high":
        verdict.blockers.append("Looks like a scam: " + "; ".join(scam.signals))
    elif scam.risk == "medium":
        verdict.notes.append("Possible scam signs: " + "; ".join(scam.signals))
    return RulesResult(
        eligible=not verdict.blockers,
        location_points=verdict.location_points if not verdict.blockers else 0,
        blockers=tuple(verdict.blockers),
        notes=tuple(verdict.notes),
        scam_risk=scam.risk,
    )
