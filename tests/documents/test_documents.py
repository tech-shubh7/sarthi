"""Tailored documents: facts come from the resume only; the PDF is readable; hostile text stays text.

People and companies are fictional.
"""

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from sarthi.ai.documents import CoverAnswer, TailorAnswer, TailoredJob
from sarthi.chat.document_report import report_text
from sarthi.documents.build import cover_letter, plain_letter, tailored_resume, without_claims
from sarthi.documents.handler import file_stem, resume_hash
from sarthi.documents.keywords import keyword_report
from sarthi.documents.render import ats_problems, letter_pdf, paper_for, pdf_text, resume_pdf
from sarthi.documents.truth import fact_problems, figure_key, new_numbers, style_problems
from sarthi.jobs.skills import same_skill, skills_in
from sarthi.profile.master import (
    Contact,
    Job,
    MasterResume,
    Project,
    School,
    SkillGroup,
    contact_from,
    grounded,
    load_master,
    name_from,
    save_master,
)

RESUME_TEXT = """Asha Rao
asha.rao@example.com | +91 98765 43210 | linkedin.com/in/asha-example | github.com/asha-example
Pune, India

Summary
Backend engineer building Python and Node.js services and RAG features.

Experience
Software Engineer, Acme Labs (Jun 2025 - Present)
- Built 12 FastAPI services handling 5k requests per minute
- Cut p95 latency by 40% with Redis caching
- Added a RAG search with LangChain and pgvector

Software Engineering Intern, Beta Corp (Jan 2025 - May 2025)
- Wrote Express.js APIs and Jest tests

Projects
DocBot: answers questions over PDFs with LangChain
- Indexed 2,000 pages in PostgreSQL

Education
B.E. Computer Engineering, Pune University (2020 - 2024)

Skills
Languages: Python, TypeScript, JavaScript
Tools: FastAPI, Node.js, Express.js, PostgreSQL, Redis, Docker, LangChain, Jest
"""


def master() -> MasterResume:
    return MasterResume(
        name="Asha Rao",
        headline="Backend Engineer",
        contact=Contact(email="asha.rao@example.com", phone="+91 98765 43210", location="Pune, India"),
        summary="Backend engineer building Python and Node.js services and RAG features.",
        experience=(
            Job(
                company="Acme Labs",
                title="Software Engineer",
                start="Jun 2025",
                end="Present",
                bullets=(
                    "Built 12 FastAPI services handling 5k requests per minute",
                    "Cut p95 latency by 40% with Redis caching",
                    "Added a RAG search with LangChain and pgvector",
                ),
            ),
            Job(
                company="Beta Corp",
                title="Software Engineering Intern",
                start="Jan 2025",
                end="May 2025",
                bullets=("Wrote Express.js APIs and Jest tests",),
            ),
        ),
        projects=(
            Project(
                name="DocBot",
                description="answers questions over PDFs",
                bullets=("Indexed 2,000 pages in PostgreSQL",),
            ),
        ),
        education=(
            School(school="Pune University", degree="B.E. Computer Engineering", start="2020", end="2024"),
        ),
        skills=(
            SkillGroup(label="Languages", items=("Python", "TypeScript", "JavaScript")),
            SkillGroup(
                label="Tools", items=("FastAPI", "Node.js", "PostgreSQL", "Redis", "Docker", "LangChain")
            ),
        ),
    )


# ---- reading the resume locally ----


def test_contact_and_name_are_read_locally() -> None:
    contact = contact_from(RESUME_TEXT)
    assert contact.email == "asha.rao@example.com"
    assert contact.phone == "+91 98765 43210"
    assert contact.links == ("linkedin.com/in/asha-example", "github.com/asha-example")
    assert name_from(RESUME_TEXT) == "Asha Rao"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("RESUME\nASHA RAO\n", "Asha Rao"),
        ("Curriculum Vitae\nAsha\n", ""),
        ("Software Engineer\nAsha Rao", "Asha Rao"),
        ("", ""),
    ],
)
def test_name_heuristic(text: str, name: str) -> None:
    assert name_from(text) == name


def test_years_are_not_phone_numbers() -> None:
    assert contact_from("2020 - 2024 2019 2025").phone == ""


def test_grounding_drops_what_the_resume_does_not_say() -> None:
    draft = master().model_copy(
        update={
            "experience": (
                *master().experience,
                Job(
                    company="Google",
                    title="Staff Engineer",
                    start="2019",
                    end="2020",
                    bullets=("Led 40 people",),
                ),
            ),
            "projects": (*master().projects, Project(name="Quantum Compiler")),
            "skills": (SkillGroup(items=("Python", "Kubernetes")),),
            "education": (School(school="Pune University", start="2018", end="2024"),),
        }
    )
    clean = grounded(draft, RESUME_TEXT)
    assert [j.company for j in clean.experience] == ["Acme Labs", "Beta Corp"]
    assert [p.name for p in clean.projects] == ["DocBot"]
    assert clean.all_skills() == ["Python"]
    assert clean.education[0].start == ""  # 2018 is not in the resume: no invented dates


def test_master_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    save_master(path, master())
    assert load_master(path) == master()
    path.write_text("{broken", encoding="utf-8")
    assert load_master(path) is None
    assert load_master(tmp_path / "missing.json") is None


# ---- truth checks ----


@pytest.mark.parametrize(("a", "b"), [("$300K+", "300k"), ("1,200", "1200"), ("40%", "40%")])
def test_figure_keys(a: str, b: str) -> None:
    assert figure_key(a) == figure_key(b)


def test_new_numbers_and_skills_are_caught() -> None:
    assert new_numbers("Cut latency by 40% for 3M users", RESUME_TEXT) == ["3M"]
    problems = fact_problems("Built services on Kubernetes with FastAPI", RESUME_TEXT, ("Python",))
    assert problems == ['new skill "Kubernetes"']
    assert fact_problems("Built 12 FastAPI services", RESUME_TEXT, ()) == []


@pytest.mark.parametrize(
    "text",
    [
        "I am passionate about backend work",
        "Leveraged Redis",
        "Here is the revised resume",
        "As an AI language model",
        "As an AI, I cannot verify this",
        "I am an AI and cannot",
        "as a large language model I",
        "Dear [Company Name]",
        "Fast — reliable",
        "My work aligns with your goals",
    ],
)
def test_style_problems(text: str) -> None:
    assert style_problems(text)


def test_plain_text_has_no_style_problems() -> None:
    assert style_problems("Built 12 FastAPI services and cut latency by 40%.") == []


@pytest.mark.parametrize(
    "text",
    [
        # What an AI engineer's own letter says (these sent good letters back as "AI talk").
        "I'd like to join Gamma as an AI Engineer.",
        "I have worked as an AI/ML engineer for a year.",
        "Built a chatbot on large language models with RAG.",
        "I'm applying as an AI developer on your platform team.",
    ],
)
def test_an_ai_engineer_is_not_an_ai_talking(text: str) -> None:
    assert style_problems(text) == []


# ---- building the tailored resume ----


def test_facts_always_come_from_the_master() -> None:
    answer = TailorAnswer(
        headline="AI Backend Engineer | Python, RAG",
        summary="Backend engineer who builds Python services and RAG features.",
        experience=[
            TailoredJob(
                index=0,
                bullets=[
                    "Added RAG search with LangChain and pgvector",
                    "Built 12 FastAPI services handling 5k requests per minute",
                    "Scaled services to 10M users",  # invented number
                    "Ran Kubernetes clusters",  # invented skill
                ],
            ),
            TailoredJob(index=7, bullets=["ignored: no such position"]),
        ],
        project_order=[0, 0, 5, -1],
        skills_first=["LangChain", "Kubernetes", "python"],
    )
    built = tailored_resume(master(), answer, RESUME_TEXT)
    job = built.resume.experience[0]
    assert (job.company, job.title, job.start, job.end) == (
        "Acme Labs",
        "Software Engineer",
        "Jun 2025",
        "Present",
    )
    assert job.bullets == (
        "Added RAG search with LangChain and pgvector",
        "Built 12 FastAPI services handling 5k requests per minute",
    )
    assert len(built.reverted) == 2
    assert built.resume.experience[1] == master().experience[1]
    assert built.resume.contact == master().contact
    assert built.resume.headline == "AI Backend Engineer | Python, RAG"
    first_group = built.resume.skills[0]
    assert first_group.items[0] == "LangChain"  # the job's skills first, only ones the person has
    assert "Kubernetes" not in built.resume.all_skills()


def test_mostly_untrue_bullets_fall_back_to_the_masters() -> None:
    answer = TailorAnswer(experience=[TailoredJob(index=0, bullets=["Led 50 engineers", "Saved $2M"])])
    built = tailored_resume(master(), answer, RESUME_TEXT)
    assert built.resume.experience[0].bullets == master().experience[0].bullets


def test_no_ai_answer_means_the_master_as_it_is() -> None:
    assert tailored_resume(master(), None, RESUME_TEXT).resume == master()


def test_style_problems_are_collected_then_reverted_when_strict() -> None:
    answer = TailorAnswer(summary="Passionate backend engineer building Python services.")
    loose = tailored_resume(master(), answer, RESUME_TEXT)
    assert loose.problems and loose.resume.summary.startswith("Passionate")
    strict = tailored_resume(master(), answer, RESUME_TEXT, strict=True)
    assert strict.resume.summary == master().summary


def test_em_dashes_are_made_plain() -> None:
    answer = TailorAnswer(summary="Backend engineer — Python and Node.js services.")
    assert (
        tailored_resume(master(), answer, RESUME_TEXT).resume.summary
        == "Backend engineer, Python and Node.js services."
    )


# ---- cover letter ----


def letter(paragraphs: list[str], strict: bool = False) -> Any:
    return cover_letter(
        master(),
        CoverAnswer(paragraphs=paragraphs),
        RESUME_TEXT,
        role="AI Engineer",
        company="Gamma",
        strict=strict,
    )


def test_true_letter_is_used_with_code_written_greeting_and_sign_off() -> None:
    result = letter(["I am applying for the AI Engineer role.", "At Acme Labs I built 12 FastAPI services."])
    assert result.from_ai
    assert result.greeting == "Dear Gamma hiring team,"
    assert result.sign_off == "Best regards,\nAsha Rao"


def test_untrue_letter_becomes_the_plain_one_and_says_why() -> None:
    result = letter(["I led a team of 20.", "I saved $5M."])
    assert not result.from_ai
    assert result.paragraphs == plain_letter(master(), "AI Engineer", "Gamma")
    assert result.problems  # handed back to the AI for one more try
    assert all(not fact_problems(p, RESUME_TEXT, master().all_skills()) for p in result.paragraphs)


def test_cliche_letter_is_retried_then_plain() -> None:
    assert letter(["I am thrilled to apply.", "I built 12 FastAPI services."]).problems
    assert not letter(["I am thrilled to apply.", "I built 12 FastAPI services."], strict=True).from_ai


def test_the_second_try_keeps_its_true_paragraphs() -> None:
    result = letter(
        [
            "I'd like to join Gamma as an AI Engineer.",
            "At Acme Labs I built 12 FastAPI services.",
            "I saved $5M.",  # not in the resume: dropped, not the whole letter
        ],
        strict=True,
    )
    assert result.from_ai
    assert result.paragraphs == [
        "I'd like to join Gamma as an AI Engineer.",
        "At Acme Labs I built 12 FastAPI services.",
    ]


def test_reviewer_claims_remove_only_their_paragraphs() -> None:
    whole = letter(
        ["Para one about Gamma.", "At Acme Labs I built 12 FastAPI services.", "I led the platform team."]
    )
    trimmed = without_claims(whole, ["I led the platform team - no such title in the resume"])
    assert trimmed is not None
    assert trimmed.paragraphs == ["Para one about Gamma.", "At Acme Labs I built 12 FastAPI services."]
    assert without_claims(whole, ["something not in the letter - x"]) is None  # cannot remove it: not trusted
    assert without_claims(whole, ["Para one - x", "At Acme Labs - y"]) is None  # too little left


def test_plain_letter_reads_like_a_person() -> None:
    text = " ".join(plain_letter(master(), "AI Engineer", "Gamma"))
    assert text.startswith(
        "I'd like to apply for the AI Engineer role at Gamma. I'm currently a Software Engineer at Acme Labs, where I "
    )
    assert "Recently:" not in text
    assert "My main skills are" not in text
    assert all(
        not fact_problems(p, RESUME_TEXT, master().all_skills())
        for p in plain_letter(master(), "AI Engineer", "Gamma")
    )


def test_plain_letter_without_experience() -> None:
    bare = MasterResume(name="Asha Rao")
    assert plain_letter(bare, "AI Engineer", "Gamma") == [
        "I'd like to apply for the AI Engineer role at Gamma.",
        "I'd be glad to talk about how I could help the team at Gamma. Thank you for your time.",
    ]


# ---- keywords ----


def test_keyword_report_separates_shown_hidden_and_missing() -> None:
    job = "We need Python, FastAPI, Docker, Kubernetes and Go (golang) experience; RAG is a plus."
    report = keyword_report(job, "Python FastAPI RAG", RESUME_TEXT, ("Docker",))
    assert report.on_resume == ["Python", "FastAPI", "RAG"]
    assert report.have_not_shown == ["Docker"]
    assert report.missing == ["Kubernetes", "Go"]
    assert report.score == 50
    assert keyword_report("No skills named here.", "", "", ()).score is None


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Java and JavaScript", ["Java", "JavaScript"]),
        ("node.js, Postgres, k8s", ["Node.js", "PostgreSQL", "Kubernetes"]),
        ("C# and C++", [".NET", "C++"]),
        ("goal-oriented", []),
        ("", []),
    ],
)
def test_skills_in(text: str, found: list[str]) -> None:
    assert skills_in(text) == found


def test_own_skills_count_even_outside_the_vocabulary() -> None:
    assert skills_in("Experience with Temporal workflows", ["Temporal"]) == ["Temporal"]
    assert same_skill("Kubernetes", "k8s")


# ---- PDFs ----


def test_resume_pdf_is_readable_by_a_tracking_system() -> None:
    pdf = resume_pdf(master())
    assert pdf.startswith(b"%PDF")
    assert ats_problems(pdf, master()) == []
    text, pages = pdf_text(pdf)
    assert pages == 1
    lines = [line.strip() for line in text.splitlines()]
    title = next(i for i, line in enumerate(lines) if line.startswith("Software Engineer"))
    assert lines[title + 1].startswith("Acme Labs")  # the employer right under the title, in reading order


@pytest.mark.parametrize(
    "hostile",
    [
        '#set page(width: 1cm) #panic("x")',
        '#read("/etc/passwd")',
        "*bold* _it_ $x^2$ @label <tag> // comment",
        "] #{ 1 + 1 } [",
        "\\ backslash \u202e rtl",
        "x" * 2000,
    ],
)
def test_hostile_text_is_printed_never_run(hostile: str) -> None:
    resume = master().model_copy(update={"summary": hostile[:1500], "name": "Asha #Rao"})
    pdf = resume_pdf(resume)
    text, _ = pdf_text(pdf)
    assert "Asha #Rao" in text
    assert "passwd" not in text or "#read" in text.replace("\n", "")


def test_letter_pdf_and_paper() -> None:
    built = letter(["I am applying for the AI Engineer role.", "I built 12 FastAPI services."])
    pdf = letter_pdf(master(), built, paper_for("US"), today=date(2026, 11, 21))
    text, _ = pdf_text(pdf)
    assert "21 November 2026" in text
    assert "Dear Gamma hiring team," in text
    assert paper_for("IN") == "a4"


def test_ats_reports_what_is_missing() -> None:
    assert ats_problems(b"not a pdf", master()) == ["the PDF has no readable text"]
    other = master().model_copy(update={"name": "Someone Else"})
    assert any("name" in p for p in ats_problems(resume_pdf(master()), other))


# ---- names and the chat report ----


@pytest.mark.parametrize(
    ("name", "company", "stem"),
    [
        ("Asha Rao", "Acme Labs", "Asha_Rao_Acme_Labs"),
        ("", "../../etc", "etc"),
        ("", "", "Sarthi"),
        ("Åsa Ö", "Zürich AG", "sa_Z_rich_AG"),
    ],
)
def test_file_stem_is_safe(name: str, company: str, stem: str) -> None:
    assert file_stem(name, company) == stem


def test_resume_hash_changes_with_the_resume() -> None:
    assert resume_hash("a") != resume_hash("b")
    assert len(resume_hash("a")) == 16


def test_report_text_escapes_and_explains() -> None:
    text = report_text(
        {
            "role": "<b>AI</b> Engineer",
            "company": "Gamma & Co",
            "keywords": {
                "on_resume": ["Python"],
                "have_not_shown": ["Docker"],
                "missing": ["Go"],
                "score": 33,
            },
            "tailored_by_ai": False,
            "letter_by_ai": False,
            "kept_original": ["x"],
            "ats": ["name cannot be read"],
        }
    )
    assert "&lt;b&gt;AI&lt;/b&gt; Engineer" in text
    assert "Gamma &amp; Co" in text
    assert "33%" in text and "Docker" in text and "Go" in text
    assert "original resume, not tailored" in text
    assert "⚠️ name cannot be read" in text
