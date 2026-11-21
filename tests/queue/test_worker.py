# Ported from JobRadar (MIT), tests/test_worker.py @ fdd739b.
import asyncio

import pytest

from sarthi.db.tasks import TaskRecord, TaskStatus, TaskStore
from sarthi.queue.tasks import DeferError, PermanentError, RetryableError, TaskQueue
from sarthi.queue.worker import Worker


async def run_until_idle(worker: Worker, store: TaskStore, timeout: float = 5) -> None:
    runner = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(timeout):
            while store.task_counts()[TaskStatus.PENDING] or store.task_counts()[TaskStatus.RUNNING]:
                await asyncio.sleep(0.01)
    finally:
        worker.stop()
        await runner


async def test_outcomes_are_recorded(queue: TaskQueue, task_store: TaskStore) -> None:
    seen: list[str] = []

    async def handler(task: TaskRecord) -> None:
        seen.append(task.key)
        if task.key == "boom":
            raise RuntimeError("temporary")
        if task.key == "bad":
            raise PermanentError("404")
        if task.key == "slow-down":
            raise RetryableError("429", retry_after=60)

    for key in ("ok", "boom", "bad", "slow-down"):
        queue.enqueue("fetch", key)
    worker = Worker(queue, {"fetch": handler}, poll_interval=0.01)

    runner = asyncio.create_task(worker.run())
    async with asyncio.timeout(5):
        while len(seen) < 4 or task_store.task_counts()[TaskStatus.RUNNING]:
            await asyncio.sleep(0.01)
    worker.stop()
    await runner

    counts = task_store.task_counts()
    assert sorted(seen) == ["bad", "boom", "ok", "slow-down"]
    assert counts[TaskStatus.DONE] == 1
    assert counts[TaskStatus.FAILED] == 1
    assert counts[TaskStatus.PENDING] == 2  # "boom" and "slow-down" are waiting to retry


async def test_defer_and_concurrency_limit(queue: TaskQueue, task_store: TaskStore) -> None:
    active = 0
    peak = 0
    deferred = False

    async def handler(task: TaskRecord) -> None:
        nonlocal active, peak, deferred
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        if task.key == "k0" and not deferred:
            deferred = True
            raise DeferError(0)

    for i in range(6):
        queue.enqueue("fetch", f"k{i}")
    await run_until_idle(Worker(queue, {"fetch": handler}, concurrency=2, poll_interval=0.01), task_store)

    assert peak == 2
    assert task_store.task_counts()[TaskStatus.DONE] == 6


async def test_stop_waits_for_in_flight(queue: TaskQueue, task_store: TaskStore) -> None:
    started = asyncio.Event()

    async def slow(task: TaskRecord) -> None:
        started.set()
        await asyncio.sleep(0.05)

    queue.enqueue("fetch", "k")
    worker = Worker(queue, {"fetch": slow}, poll_interval=0.01)
    runner = asyncio.create_task(worker.run())
    await started.wait()
    worker.stop()
    await runner
    assert task_store.task_counts()[TaskStatus.DONE] == 1


async def test_cancelling_the_worker_hands_running_tasks_back(
    queue: TaskQueue, task_store: TaskStore
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def stuck(task: TaskRecord) -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # e.g. waiting on an AI provider
        except asyncio.CancelledError:
            cancelled.set()
            raise

    queue.enqueue("fetch", "k")
    worker = Worker(queue, {"fetch": stuck}, poll_interval=0.01)
    runner = asyncio.create_task(worker.run())
    await started.wait()
    await asyncio.sleep(0.03)  # the loop is back waiting for work, not on the handler
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    # Shutdown closes the database next: by now the handler has stopped and the task is back in the queue,
    # with no attempt used up.
    assert cancelled.is_set()
    counts = task_store.task_counts()
    assert (counts[TaskStatus.RUNNING], counts[TaskStatus.PENDING], counts[TaskStatus.FAILED]) == (0, 1, 0)


async def test_wake_skips_the_poll_wait(queue: TaskQueue, task_store: TaskStore) -> None:
    done = asyncio.Event()

    async def handler(task: TaskRecord) -> None:
        done.set()

    worker = Worker(queue, {"fetch": handler}, poll_interval=60)
    runner = asyncio.create_task(worker.run())
    await asyncio.sleep(0.01)  # worker is now waiting for up to 60 s
    queue.enqueue("fetch", "k")
    worker.wake()
    async with asyncio.timeout(2):
        await done.wait()
    worker.stop()
    await runner


def test_a_worker_needs_handlers(queue: TaskQueue) -> None:
    with pytest.raises(ValueError, match="at least one handler"):
        Worker(queue, {})
