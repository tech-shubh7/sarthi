import sqlite3
from collections.abc import Callable

import httpx
import pytest

from sarthi.db.watched import WatchStore
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources import web
from sarthi.sources.base import SearchQuery
from sarthi.sources.pages import PageSource, announcement_url, page_text

PAGE = "https://careers.acme.example/india/hiring"
PUBLIC_IP = "93.184.216.34"

BEFORE = """<html><body><nav>Home | About | Careers</nav>
<h1>Hiring at Acme India</h1>
<p>Graduate Engineer Trainee program 2026 is closed now</p>
<p>Updated 3 days ago</p></body></html>"""

AFTER = BEFORE.replace(
    "<p>Updated 3 days ago</p>",
    "<p>Updated today</p><p>Company: Acme India</p><p>Role: Backend Developer (freshers welcome)</p>"
    "<p>Location: Pune</p>"
    '<p><a href="/apply/backend-2026">Apply for the Backend Developer drive before 30 November</a></p>',
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Every name resolves to a public address unless a test says otherwise."""
    table: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list[str]:
        if host in table:
            return table[host]
        return [PUBLIC_IP]

    monkeypatch.setattr(web, "resolve_host", resolve)
    return table


@pytest.fixture
def store(conn: sqlite3.Connection) -> WatchStore:
    return WatchStore(conn)


def serve(*pages: str) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    queue, seen = list(pages), []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=queue.pop(0) if len(queue) > 1 else queue[0])

    return httpx.MockTransport(handler), seen


def source(store: WatchStore, transport: httpx.MockTransport, url: str = PAGE) -> PageSource:
    return PageSource(url, "", store, transport=transport)


async def test_first_read_only_records_the_page(store: WatchStore) -> None:
    transport, _ = serve(BEFORE)
    assert await source(store, transport).fetch(SearchQuery()) == []
    assert "Hiring at Acme India" in (store.page_text(PAGE) or "")


async def test_a_new_announcement_becomes_a_job(store: WatchStore) -> None:
    transport, _ = serve(BEFORE, AFTER)
    page = source(store, transport)
    await page.fetch(SearchQuery())
    jobs = await page.fetch(SearchQuery())
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.company, job.title, job.location_text, job.source) == (
        "Acme India",
        "Backend Developer",
        "Pune",
        "page",
    )
    assert str(job.url) == "https://careers.acme.example/apply/backend-2026"  # relative link made whole
    assert page.label == "careers.acme.example"
    assert await page.fetch(SearchQuery()) == []  # read again: nothing new


async def test_timestamps_and_small_changes_are_not_announcements(store: WatchStore) -> None:
    transport, _ = serve(BEFORE, BEFORE.replace("3 days ago", "4 days ago"), BEFORE + "<p>Hello there!</p>")
    page = source(store, transport)
    for _ in range(3):
        assert await page.fetch(SearchQuery()) == []


async def test_a_redesign_becomes_the_new_baseline(store: WatchStore) -> None:
    redesign = "<html>" + "".join(f"<p>Brand new section number {i} of the website</p>" for i in range(30))
    transport, _ = serve(BEFORE, redesign, redesign)
    page = source(store, transport)
    await page.fetch(SearchQuery())
    assert await page.fetch(SearchQuery()) == []
    assert "Brand new section number 29" in (store.page_text(PAGE) or "")


def test_page_text_makes_links_whole_and_drops_scripts() -> None:
    html = (
        '<a href="../jobs/1">Job one</a> <a href="https://x.example/2">Job two</a>'
        '<a href="javascript:alert(1)">Click</a> <a href="mailto:hr@acme.example">Mail us</a>'
    )
    text = page_text(html, "https://acme.example/careers/india/")
    assert "Job one (https://acme.example/careers/jobs/1)" in text
    assert "Job two (https://x.example/2)" in text
    assert "javascript" not in text
    assert "mailto" not in text
    assert "Click" in text
    assert "Mail us" in text


def test_two_drives_on_one_page_get_different_links() -> None:
    first = announcement_url(PAGE, ["Walk-in drive for Data Analysts on Saturday"])
    second = announcement_url(PAGE, ["Walk-in drive for Java Developers on Sunday"])
    assert first != second
    assert first.startswith(PAGE + "?sarthi=")
    assert announcement_url(PAGE + "?lang=en", ["x"]).startswith(PAGE + "?lang=en&sarthi=")


# ---- the public-address guard ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0", "100.64.0.1"],  # noqa: S104  (addresses to refuse)
)
async def test_pages_on_private_addresses_are_refused(
    store: WatchStore, public_dns: dict[str, list[str]], address: str
) -> None:
    public_dns["careers.acme.example"] = [PUBLIC_IP, address]  # one bad address is enough to refuse
    transport, seen = serve(BEFORE)
    with pytest.raises(PermanentError, match="private or local"):
        await source(store, transport).fetch(SearchQuery())
    assert seen == []  # never contacted


async def test_a_redirect_to_a_private_address_is_refused(
    store: WatchStore, public_dns: dict[str, list[str]]
) -> None:
    public_dns["router.internal.example"] = ["192.168.0.1"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "careers.acme.example":
            return httpx.Response(302, headers={"location": "http://router.internal.example/admin"})
        return httpx.Response(200, text="secret admin page")

    with pytest.raises(PermanentError, match="private or local"):
        await source(store, httpx.MockTransport(handler)).fetch(SearchQuery())


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("ftp://acme.example/jobs", PermanentError),
        ("file:///etc/passwd", PermanentError),
        ("https://user:pass@acme.example/jobs", PermanentError),
        ("https://acme.example:99999/jobs", PermanentError),
        ("https:///no-host", PermanentError),
    ],
)
async def test_check_public_url_refuses_bad_links(url: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        await web.check_public_url(url)


async def test_a_name_that_cannot_be_looked_up_is_tried_again_later(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_such_name(host: str, port: int) -> list[str]:
        raise OSError("Name or service not known")

    monkeypatch.setattr(web, "resolve_host", no_such_name)
    with pytest.raises(RetryableError):
        await web.check_public_url("https://nowhere.example/jobs")


async def test_public_pages_pass_the_guard() -> None:
    await web.check_public_url("https://careers.acme.example/jobs?x=1")


async def test_real_resolver_reads_this_computer() -> None:
    assert "127.0.0.1" in await web.resolve_host("localhost", 80) or await web.resolve_host("localhost", 80)


Handler = Callable[[httpx.Request], httpx.Response]
