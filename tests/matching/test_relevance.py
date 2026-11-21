"""Is the job really for this person? Titles below are real ones a 1-year Python/Node.js/AI developer was sent
before these rules existed (companies are fictional)."""

from datetime import timedelta
from typing import Any

import pytest

from sarthi.ai.scoring import MatchAnswers, blocker_for
from sarthi.jobs.geo import seniority_of
from sarthi.jobs.requirements import min_years_in_title, too_experienced_for, years_tolerance
from sarthi.jobs.stack import missing_stack, techs_in
from sarthi.matching.decide import Decision
from sarthi.matching.funnel import evaluate
from sarthi.matching.prefilter import title_words
from sarthi.matching.rules import check_rules
from tests.matching.conftest import NOW, make_job, make_profile

SKILLS = [
    "Python",
    "TypeScript",
    "JavaScript",
    "Node.js",
    "FastAPI",
    "React.js",
    "PostgreSQL",
    "LangChain",
    "RAG",
]
ROLES = [
    "Backend Engineer",
    "AI Engineer",
    "Node.js Engineer",
    "Machine Learning Engineer",
    "Software Engineer",
]
DESCRIPTION = (
    "Build APIs with Python, FastAPI, Node.js and PostgreSQL; RAG with LangChain; React.js front end."
)


def junior(**overrides: Any) -> Any:
    return make_profile(roles=ROLES, keywords=SKILLS, years_experience=1, **overrides)


async def verdict(title: str, description: str = DESCRIPTION, **job: Any) -> tuple[Decision, str]:
    result = await evaluate(make_job(title=title, description=description, **job), junior(), NOW)
    return result.decision, result.outcome.hidden_because or ""


# ---- level read from the title --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Software Engineer III - Backend", "senior"),
        ("SDE III", "senior"),
        ("SDE-3", "senior"),
        ("Data Scientist IV", "senior"),
        ("Software Engineer (L5)", "senior"),
        ("Sr. Software Engineer - Bangalore", "senior"),
        ("Senior Backend Engineer", "senior"),
        ("Senior-level Python dev", "senior"),
        ("Staff Engineer", "lead"),
        ("AI Automation Technical Leader", "lead"),
        ("Principal Architect", "principal"),
        ("Software Engineer II", "mid"),
        ("SDE 2", "mid"),
        ("Engineer 2", "mid"),
        ("Software Engineer I", "junior"),
        ("SDE-1", "junior"),
        ("Associate Software Developer", "junior"),
        ("Assoc Software Engineer II", "junior"),
        ("Assoc. Data Analyst", "junior"),
        ("Software Engineering Manager", "manager"),
        ("Associate Director, Engineering", "manager"),
        ("Backend Engineer", None),
        ("AI Engineer", None),
        ("Seniority Analyst", None),
        ("Engineer, Infrastructure", None),
        ("AI Engineer 5-8 Yrs", None),
        ("Engineer 3+ years", None),
        ("", None),
    ],
)
def test_seniority_of(title: str, level: str | None) -> None:
    assert seniority_of(title) == level


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer III - Backend",
        "Senior Backend Engineer",
        "Sr. Software Engineer - Bangalore",
        "Staff AI Engineer",
        "Lead Node.js Engineer",
        "Principal Machine Learning Engineer",
    ],
)
async def test_senior_titles_are_hidden_for_one_year(title: str) -> None:
    decision, why = await verdict(title)
    assert decision is Decision.HIDE
    assert "level role; you have 1" in why


@pytest.mark.parametrize(
    "title",
    [
        # Real titles a 1-year developer was sent (2026-11-21): "II" roles ask for 2-3 years.
        "Software Engineer II",
        "Software Development Engineer II - Machine Learning",
        "Applied AI Engineer II",
        "ML Engineer II",
        "Software Engineer 2, Managed Gateways",
        "SDE 2",
        # Managing other engineers is a level, for someone who does not target manager roles.
        "Software Engineering Manager",
        "Senior Engineering Manager - AI & Python",
        "Full Stack Developer, Backend-First (Director / Senior Director / AVP Technology)",
        "AI Engineer – Team Lead / Manager (AI Transformation & Automation)",
    ],
)
async def test_mid_level_and_manager_titles_are_hidden_for_one_year(title: str) -> None:
    decision, why = await verdict(title)
    assert decision is Decision.HIDE, (title, why)
    assert "level role; you have 1" in why


async def test_levels_within_reach_are_kept() -> None:
    for title in ("Associate AI Engineer", "Assoc Software Engineer II", "Backend Engineer", "SDE-1"):
        decision, why = await verdict(title)
        assert decision is not Decision.HIDE, (title, why)


def test_mid_level_is_fine_from_two_years() -> None:
    assert check_rules(make_job(title="Software Engineer II"), make_profile(years_experience=2)).eligible
    assert not check_rules(make_job(title="Software Engineer II"), make_profile(years_experience=1)).eligible


def test_manager_titles_are_kept_for_people_who_want_them() -> None:
    profile = make_profile(roles=["Engineering Manager"], years_experience=1)
    assert check_rules(make_job(title="Software Engineering Manager"), profile).eligible


def test_tolerance_is_tighter_early_in_a_career() -> None:
    assert years_tolerance(1) == 1
    assert years_tolerance(5) == 2
    assert too_experienced_for(3, 1)
    assert not too_experienced_for(2, 1)
    assert not too_experienced_for(7, 5)


def test_senior_is_fine_with_enough_experience() -> None:
    result = check_rules(make_job(title="Senior Backend Engineer"), make_profile(years_experience=4))
    assert result.eligible


# ---- years asked for in the title or the text ------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "years"),
    [
        ("Java Support Engineer - (8-10) YRS", 8),
        ("Generative AI 5- 9yrs", 5),
        ("Tcs Hiring Data Scientist Gen AI  5 To 12 Years", 5),
        ("AWS Production Support Software Engineer - INF 8-10 yrs", 8),
        ("Agile Scrum Master_4+Yrs_Pune", 4),
        ("AI Engineer", None),
        ("Python 3.12 Engineer", None),
        ("Web3 Engineer", None),
    ],
)
def test_min_years_in_title(title: str, years: int | None) -> None:
    assert min_years_in_title(title) == years


async def test_years_in_the_text_beyond_reach_hide_the_job() -> None:
    decision, why = await verdict("Backend Engineer", DESCRIPTION + " Requires 3-5 years of experience.")
    assert (decision, why) == (Decision.HIDE, "Asks for 3+ years of experience; you have 1")
    decision, _ = await verdict("Backend Engineer", DESCRIPTION + " 2+ years of experience preferred.")
    assert decision is not Decision.HIDE


async def test_years_in_the_title_hide_the_job() -> None:
    decision, why = await verdict("AI Engineer 5-8 Yrs")
    assert decision is Decision.HIDE
    assert why.startswith("Asks for 5+ years")


# ---- the technology a title is built around -------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "missing"),
    [
        ("Java Developer", {"Java"}),
        ("Software Engineer (.NET)", {".NET"}),
        (".NET Full Stack Developer", {".NET"}),
        ("ASP.NET Core Developer - MVC, WEB API, SQL Server", {".NET"}),
        ("dotnet full stack Developer with Gen Ai", {".NET"}),
        ("C# Developer", {".NET"}),
        ("Php Full Stack Developer", {"PHP"}),
        ("PHP Laravel Developer", {"PHP"}),
        ("Sap Abap Developer", {"SAP"}),
        ("Golang Developer", {"Go"}),
        ("iOS Developer", {"Swift"}),
        ("Senior C++ Engineer", {"C++"}),
        ("React Native Developer", {"React Native"}),
        ("Software Engineer - Java & AI Automation (Java and Python)", set()),
        ("Full-Stack Developer (React.js / Next.js / Node.js)", set()),
        ("JavaScript Developer", set()),
        ("Java Script Developer", set()),
        ("MERN Stack Developer", set()),
        ("Data Engineer - PySpark", set()),
        ("AI Engineer", set()),
        ("Trust & Safety Analyst", set()),
    ],
)
def test_missing_stack(title: str, missing: set[str]) -> None:
    assert missing_stack(title, SKILLS) == missing


def test_stack_needs_the_skill_not_just_the_word() -> None:
    assert missing_stack("Java Developer", ["Java", "Spring"]) == set()
    assert missing_stack("Java Developer", []) == {"Java"}
    assert techs_in("") == set()


async def test_other_stacks_are_hidden_with_a_reason() -> None:
    decision, why = await verdict(".NET Full Stack Developer", "C#, Angular, SQL Server. " + DESCRIPTION)
    assert decision is Decision.HIDE
    assert why == "Built around .NET, which is not in your skills"


# ---- other job families and interns ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Sales Specialist - Data & AI Security", "sales"),
        ("Social Media Associate", "marketing"),
        ("US Recruiter", "recruiting"),
        ("Software Application Support Engineer", "support"),
        ("L2 Support Engineer", "support"),
        ("Desktop Support Engineer", "support"),
        ("AI/ML , Cancer Genomics & Computational Biology Trainer", "teaching"),
        ("Agile Scrum Master", "project management"),
        ("Field Service Engineer", "operations"),
    ],
)
def test_other_job_families_are_hidden(title: str, family: str) -> None:
    result = check_rules(make_job(title=title), junior())
    assert not result.eligible
    assert result.blockers[-1] == f"Looks like a {family} job, not one of your target roles"


def test_a_family_in_the_users_own_roles_is_kept() -> None:
    profile = make_profile(roles=["Support Engineer"], years_experience=1)
    assert check_rules(make_job(title="L2 Support Engineer"), profile).eligible


async def test_intern_in_the_title_wins_over_the_source() -> None:
    decision, why = await verdict("MERN Stack Developer Intern", employment_type="full_time")
    assert (decision, why) == (Decision.HIDE, "Internship role")


# ---- titles and relevance --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "words"),
    [
        ("GEN AI Developer", ["genai", "ai", "engineer"]),
        ("AI/ML Developers", ["ai", "ml", "engineer"]),
        ("Machine Learning Engineer", ["ml", "engineer"]),
        ("Back-end Developer", ["backend", "engineer"]),
        ("Node JS Developer", ["node.js", "js", "engineer"]),
        ("SW Development Engineer 1", ["software", "development", "engineer"]),
    ],
)
def test_title_synonyms(title: str, words: list[str]) -> None:
    assert title_words(title) == words


@pytest.mark.parametrize(
    "title",
    ["Integration Engineer", "BIM Engineer(CAD TO BIM)", "Embedded Hardware Engineer", "Patent Analyst"],
)
async def test_sharing_only_engineer_is_not_a_match(title: str) -> None:
    decision, why = await verdict(title, "Kubernetes and Terraform.")
    assert decision is Decision.HIDE, why


async def test_an_unrelated_title_with_few_skills_is_hidden() -> None:
    decision, why = await verdict("Veeva Vault Configuration Engineer", "Veeva Vault, Python scripts.")
    assert (decision, why) == (
        Decision.HIDE,
        "Not one of your target roles, and it asks for few of your skills",
    )


async def test_skills_alone_never_make_an_instant_alert() -> None:
    result = await evaluate(make_job(title="Engineer, SVT", description=DESCRIPTION * 3), junior(), NOW)
    assert result.decision is Decision.DIGEST


async def test_synonym_titles_match_fully() -> None:
    result = await evaluate(make_job(title="GEN AI Developer", description=DESCRIPTION), junior(), NOW)
    assert result.decision is Decision.ALERT
    assert any("Title matches" in r for r in result.reasons)


async def test_location_and_recency_alone_do_not_lift_a_job() -> None:
    decision, _ = await verdict("Customer Delight Associate", "Phones.", posted_at=NOW - timedelta(hours=1))
    assert decision is Decision.HIDE


# ---- the AI's own reading -------------------------------------------------------------------------------


def answers(**overrides: Any) -> MatchAnswers:
    data = {
        "eligible": 0.8,
        "role_fit": 0.9,
        "skills_fit": 0.9,
        "seniority_fit": 0.8,
        "overall_fit": 0.9,
        "confidence": 0.9,
    }
    return MatchAnswers.model_validate(data | overrides)


def test_ai_years_beyond_reach_block() -> None:
    assert blocker_for(answers(min_years_required=5), 1) == "Asks for 5+ years of experience; you have 1"
    assert blocker_for(answers(min_years_required=2), 1) is None
    assert blocker_for(answers(min_years_required=5), None) is None


def test_ai_level_verdict_counts_only_when_it_is_sure() -> None:
    assert blocker_for(answers(seniority_fit=0.1), 1) == "Level is well above your 1 year(s) of experience"
    assert blocker_for(answers(seniority_fit=0.1, confidence=0.3), 1) is None  # vague or empty posting


@pytest.mark.parametrize("value", [-1, 41, "lots"])
def test_ai_years_out_of_range_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="min_years_required"):
        answers(min_years_required=value)
