from datetime import UTC, datetime
from typing import Any

import pytest

from sarthi.ai.client import AiUnavailableError
from sarthi.sources.posts import (
    PostAnswer,
    absolute_links,
    added_lines,
    clean_company,
    clean_role,
    is_redesign,
    labels,
    line_key,
    links,
    page_lines,
    plain_line,
    post_to_job,
    rule_fields,
)

POSTED = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

LABELLED = """🚀 𝗛𝗶𝗿𝗶𝗻𝗴 𝗔𝗹𝗲𝗿𝘁
🏢 Company: HARMAN (a Samsung company)
💼 Role - Software Intern | R-56145-2026
📍 Location: Bengaluru
🎓 Experience: 0-1 years
Apply: https://jobs.lever.co/harman/abc
Join our channel: https://t.me/somejobs
"""


class FakeAi:
    """Stands in for AiClient: answers the post prompt, or fails."""

    def __init__(self, answer: PostAnswer | Exception, *, available: bool = True) -> None:
        self.answer = answer
        self.available = available
        self.calls = 0

    async def structured(self, prompt: Any, schema: Any, **data: str) -> PostAnswer:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    ("line", "plain"),
    [
        ("🏢 Company: Wipro", "Company: Wipro"),
        ("• Role - Backend", "Role - Backend"),
        ("𝗥𝗼𝗹𝗲: 𝗗𝗲𝘃𝗲𝗹𝗼𝗽𝗲𝗿", "Role: Developer"),  # decorative bold letters
        ("   ", ""),
        ("✅✅✅", ""),
    ],
)
def test_plain_line(line: str, plain: str) -> None:
    assert plain_line(line) == plain


@pytest.mark.parametrize(
    ("value", "company"),
    [
        ("HARMAN (a Samsung company)", "HARMAN"),
        ("  Wipro Ltd. ", "Wipro Ltd"),
        ("X", None),
        ("A" * 81, None),
        ("(only brackets)", None),
    ],
)
def test_clean_company(value: str, company: str | None) -> None:
    assert clean_company(value) == company


@pytest.mark.parametrize(
    ("value", "role"),
    [
        ("Software Intern | R-56145-2026", "Software Intern"),
        ("Multiple roles", None),
        ("Various positions across India", None),
        ("Backend Developer", "Backend Developer"),
        ("B" * 121, None),
    ],
)
def test_clean_role(value: str, role: str | None) -> None:
    assert clean_role(value) == role


def test_labels_take_the_first_of_each_kind_and_skip_links() -> None:
    found = labels("Company: Acme\nCompany: Other\nRole: https://acme.com/apply\nPosition: Data Analyst")
    assert found == {"company": "Acme", "role": "Data Analyst"}


def test_rule_fields_from_labelled_lines() -> None:
    fields = rule_fields(LABELLED)
    assert (fields.company, fields.title, fields.location, fields.experience) == (
        "HARMAN",
        "Software Intern",
        "Bengaluru",
        "0-1 years",
    )


@pytest.mark.parametrize(
    ("post", "company", "title"),
    [
        ("Infosys is hiring Java Developers!\nApply: https://x.com/a", "Infosys", "Java Developers"),
        ("Zoho is hiring for a Product Designer", "Zoho", "Product Designer"),
        ("TCS is hiring freshers\nhttps://x.com", "", ""),  # who, not what
        ("Wipro is hiring in Pune", "", ""),
        ("Amazon is hiring https://amazon.jobs/x", "", ""),  # a link is not a role
        ("Line one\nLine two\nLine three\nAcme is hiring Engineers", "", ""),  # only the first 3 lines
    ],
)
def test_is_hiring_lines(post: str, company: str, title: str) -> None:
    fields = rule_fields(post)
    assert (fields.company, fields.title) == (company, title)


def test_links_put_application_forms_first_and_drop_duplicates() -> None:
    post = "Details https://blog.example.com/post. Apply https://forms.gle/abc, again https://forms.gle/abc"
    assert links(post) == ["https://forms.gle/abc", "https://blog.example.com/post"]


async def test_a_labelled_post_becomes_a_job_without_the_ai() -> None:
    ai = FakeAi(PostAnswer(is_job=False))
    job = await post_to_job(LABELLED, source="telegram", posted_at=POSTED, ai=ai, post_id="somejobs/42")  # type: ignore[arg-type]
    assert job is not None
    assert (job.company, job.title, job.location_text) == ("HARMAN", "Software Intern", "Bengaluru")
    assert str(job.url) == "https://jobs.lever.co/harman/abc"  # the hiring system, not the channel ad
    assert (job.source, job.source_job_id, job.posted_at) == ("telegram", "somejobs/42", POSTED)
    assert ai.calls == 0


async def test_a_post_the_rules_cannot_read_goes_to_the_ai() -> None:
    post = "Great news! Our client, a fintech in Pune, needs a React developer. https://acme.example/jobs/1"
    ai = FakeAi(PostAnswer(is_job=True, company="Acme Fintech", title="React Developer", location="Pune"))
    job = await post_to_job(post, source="telegram", posted_at=POSTED, ai=ai)  # type: ignore[arg-type]
    assert job is not None
    assert (job.company, job.title, job.location_text, ai.calls) == (
        "Acme Fintech",
        "React Developer",
        "Pune",
        1,
    )


@pytest.mark.parametrize(
    "ai",
    [
        None,
        FakeAi(PostAnswer(is_job=False)),  # an ad for a course
        FakeAi(PostAnswer(is_job=True, company="", title="")),  # a job, but nothing to name it by
        FakeAi(AiUnavailableError("down")),
        FakeAi(PostAnswer(is_job=True, company="A", title="B"), available=False),
    ],
)
async def test_posts_that_cannot_be_read_are_dropped(ai: FakeAi | None) -> None:
    post = "Free webinar on cracking interviews! Register: https://example.com/webinar"
    assert await post_to_job(post, source="telegram", posted_at=POSTED, ai=ai) is None  # type: ignore[arg-type]


async def test_a_post_needs_a_link_or_a_fallback() -> None:
    post = "Company: Acme\nRole: Backend Developer\nSend your CV to hr@acme.example"
    assert await post_to_job(post, source="telegram", posted_at=POSTED) is None
    job = await post_to_job(post, source="telegram", posted_at=POSTED, fallback_url="https://t.me/jobs/7")
    assert job is not None
    assert str(job.url) == "https://t.me/jobs/7"


async def test_hostile_posts_never_raise() -> None:
    for post in (
        "",
        "\x00\x01",
        "Company: " + "A" * 5000,
        "Role: <script>x</script>\nhttps://x.example/a",
        "💥" * 3000,
    ):
        await post_to_job(post, source="telegram", posted_at=None, fallback_url="https://t.me/a/1")


# ---- page changes ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Posted 3 days ago",
        "Last updated: 21 Nov 2026",
        "© 2026 Acme Corp",
        "1,234 jobs found",
        "10:30 AM",
        "Short line",
    ],
)
def test_furniture_and_timestamps_are_not_content(line: str) -> None:
    assert line_key(line) == ""


def test_the_same_announcement_with_a_new_timestamp_is_not_new() -> None:
    assert line_key("Hiring drive for Software Engineers - posted 2 days ago") == line_key(
        "Hiring drive for Software Engineers - posted 5 days ago"
    )


def test_added_lines_ignore_moves_and_repeats() -> None:
    old = ["Hiring drive for Software Engineers in Pune", "Graduate Engineer Trainee program 2026"]
    new = [
        "Graduate Engineer Trainee program 2026",  # moved, not new
        "Hiring drive for Software Engineers in Pune",
        "Walk-in drive for Data Analysts in Chennai on Saturday",
        "Walk-in drive for Data Analysts in Chennai on Saturday",  # said twice
    ]
    assert added_lines(old, new) == ["Walk-in drive for Data Analysts in Chennai on Saturday"]


def test_a_page_that_changed_almost_entirely_is_a_redesign() -> None:
    new = ["A completely new layout of the careers website here", "Another brand new paragraph of text"]
    assert is_redesign(new, new)
    assert not is_redesign(new, new[:0])
    assert not is_redesign([], [])


def test_page_lines_collapse_spaces_and_cap_the_size() -> None:
    assert page_lines("  a   b \n\n\t c ") == ["a b", "c"]
    assert sum(len(line) for line in page_lines(("x" * 1000 + "\n") * 200)) <= 50_000


def test_absolute_links_skip_the_page_itself() -> None:
    lines = ["Apply (https://acme.example/careers/)", "Form (https://forms.gle/x)", "Details (/jobs/1)"]
    assert absolute_links(lines, "https://acme.example/careers") == ["https://forms.gle/x"]
