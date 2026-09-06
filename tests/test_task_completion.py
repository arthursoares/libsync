"""Focused cancellation classification tests for owned task drainage."""

import asyncio

import pytest

from backend.services.tasks import await_task_completion


async def _owned_work(
    started: asyncio.Event,
    release: asyncio.Event,
    finished: asyncio.Event,
) -> str:
    started.set()
    await release.wait()
    finished.set()
    return "finished"


async def test_cancel_requested_immediately_before_helper_is_propagated_after_drain():
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    owned = asyncio.create_task(_owned_work(started, release, finished))

    async def caller():
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        return await await_task_completion(owned, operation="pre-cancelled caller")

    caller_task = asyncio.create_task(caller())
    try:
        await started.wait()
        await asyncio.sleep(0)
        assert not caller_task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller_task
    finally:
        release.set()
        await asyncio.gather(owned, caller_task, return_exceptions=True)

    assert finished.is_set()


@pytest.mark.parametrize("suppress_inner_cancellation", [False, True])
async def test_repeated_outer_cancellation_is_never_suppressed(
    suppress_inner_cancellation,
):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    helper_entered = asyncio.Event()
    owned = asyncio.create_task(_owned_work(started, release, finished))

    async def caller():
        helper_entered.set()
        return await await_task_completion(
            owned,
            operation="repeated outer cancellation",
            suppress_inner_cancellation=suppress_inner_cancellation,
        )

    caller_task = asyncio.create_task(caller())
    try:
        await started.wait()
        await helper_entered.wait()
        await asyncio.sleep(0)
        caller_task.cancel()
        await asyncio.sleep(0)
        caller_task.cancel()
        await asyncio.sleep(0)
        assert not caller_task.done()
        assert not owned.cancelled()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller_task
    finally:
        release.set()
        await asyncio.gather(owned, caller_task, return_exceptions=True)

    assert finished.is_set()


@pytest.mark.parametrize("suppress_inner_cancellation", [False, True])
async def test_already_cancelled_inner_future_terminates_without_spinning(
    suppress_inner_cancellation,
):
    owned = asyncio.create_task(asyncio.Event().wait())
    owned.cancel()
    await asyncio.gather(owned, return_exceptions=True)

    async def caller():
        return await await_task_completion(
            owned,
            operation="inner cancellation",
            suppress_inner_cancellation=suppress_inner_cancellation,
        )

    caller_task = asyncio.create_task(caller())

    if suppress_inner_cancellation:
        assert await asyncio.wait_for(caller_task, timeout=0.5) is None
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller_task, timeout=0.5)


async def test_pre_cancelled_caller_is_not_suppressed_by_cancelled_inner_future():
    owned = asyncio.create_task(asyncio.Event().wait())
    owned.cancel()
    await asyncio.gather(owned, return_exceptions=True)

    async def caller():
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        return await await_task_completion(
            owned,
            operation="simultaneous outer and inner cancellation",
            suppress_inner_cancellation=True,
        )

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.create_task(caller()), timeout=0.5)
