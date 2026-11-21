# Adapted from JobRadar (MIT), src/jobradar/queue/worker.py @ fdd739b.
# Copyright (c) ThePlator. Modifications (c) Sarthi contributors.
# Changes: renamed Defer -> DeferError; log messages carry the context inline.
"""Async worker loop: claims tasks, runs their handlers with bounded concurrency, applies retry/backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping

from sarthi.db.tasks import TaskRecord
from sarthi.queue.tasks import DeferError, PermanentError, RetryableError, TaskQueue

log = logging.getLogger(__name__)

Handler = Callable[[TaskRecord], Awaitable[None]]


class Worker:
    """Runs handlers for a set of task types.

    A handler signals the outcome by returning (done) or raising: ``DeferError`` (postpone, attempt not
    counted), ``PermanentError`` (fail now), ``RetryableError`` or any other exception (retry with backoff,
    failed after the maximum attempts).
    """

    def __init__(
        self,
        queue: TaskQueue,
        handlers: Mapping[str, Handler],
        concurrency: int = 4,
        poll_interval: float = 1.0,
        name: str = "worker",
    ) -> None:
        """Serve ``handlers`` (task type -> coroutine) with at most ``concurrency`` tasks at once."""
        if not handlers:
            raise ValueError("a worker needs at least one handler")
        self.queue = queue
        self.handlers = dict(handlers)
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.name = name
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()

    def wake(self) -> None:
        """Skip the poll wait, e.g. right after enqueueing work for this worker."""
        self._wake.set()

    def stop(self) -> None:
        """Stop claiming new tasks; ``run`` returns once in-flight tasks finish."""
        self._stopping.set()
        self._wake.set()

    async def run(self) -> None:
        """Process tasks until ``stop()``; then wait for in-flight tasks to finish.

        Cancelling ``run`` cancels the in-flight tasks too and waits until each has handed its task back, so
        nothing is still running when the caller closes the database and connections.
        """
        slots = asyncio.Semaphore(self.concurrency)
        types = list(self.handlers)
        try:
            while not self._stopping.is_set():
                await slots.acquire()
                task = None if self._stopping.is_set() else self.queue.claim(types)
                if task is None:
                    slots.release()
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                    continue
                running = asyncio.create_task(self._run_one(task), name=f"{self.name}:{task.type}")
                self._in_flight.add(running)
                running.add_done_callback(self._in_flight.discard)
                running.add_done_callback(lambda _: slots.release())
            if self._in_flight:
                await asyncio.gather(*self._in_flight, return_exceptions=True)
        except asyncio.CancelledError:
            in_flight = list(self._in_flight)
            for running in in_flight:
                running.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    async def _run_one(self, task: TaskRecord) -> None:
        where = f"{task.type}[{task.key}] attempt {task.attempts}"
        try:
            await self.handlers[task.type](task)
        except DeferError as deferred:
            log.info("%s deferred %.0fs", where, deferred.seconds)
            self.queue.defer(task, deferred.seconds)
        except PermanentError as error:
            log.warning("%s failed permanently: %s", where, error)
            self.queue.fail(task, f"{type(error).__name__}: {error}")
        except asyncio.CancelledError:
            self.queue.defer(task, 0)  # shutting down: hand it back untouched
            raise
        except Exception as error:  # a handler bug must never kill the worker; the task is retried
            retry_after = error.retry_after if isinstance(error, RetryableError) else None
            retried = self.queue.retry(task, f"{type(error).__name__}: {error}", retry_after)
            log.log(
                logging.WARNING if retried else logging.ERROR,
                "%s %s: %s",
                where,
                "error, will retry" if retried else "failed after max attempts",
                error,
                exc_info=not isinstance(error, RetryableError),
            )
        else:
            self.queue.complete(task)
