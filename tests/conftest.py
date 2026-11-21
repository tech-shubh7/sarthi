import os
import random
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sarthi.ai.limits import RATE_LIMITS
from sarthi.db.connection import connect
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskStore
from sarthi.queue.tasks import TaskQueue


class FakeClock:
    """A clock tests can move forward."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def no_real_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the developer's .env or exported SARTHI_* variables."""
    for name in list(os.environ):
        if name.startswith("SARTHI_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(Path(__file__).parent)


@pytest.fixture(autouse=True)
def fresh_rate_limits() -> None:
    """Every test starts with no remembered provider limits (one test's refusal must not block the next)."""
    RATE_LIMITS._windows.clear()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "data" / "sarthi.db")
    yield connection
    connection.close()


@pytest.fixture
def task_store(conn: sqlite3.Connection, clock: FakeClock) -> TaskStore:
    return TaskStore(conn, clock=clock)


@pytest.fixture
def queue(task_store: TaskStore, clock: FakeClock) -> TaskQueue:
    return TaskQueue(task_store, clock=clock, rng=random.Random(0))


@pytest.fixture
def job_store(conn: sqlite3.Connection, clock: FakeClock) -> JobStore:
    return JobStore(conn, clock=clock)
