import asyncio

import pytest

from modus.desktop.approvals import RunApprovalBroker


@pytest.mark.asyncio
async def test_broker_allow_lifecycle_is_stable_over_many_runs():
    broker = RunApprovalBroker()

    for index in range(100):
        run_id = f"run_{index}"
        approval_id = f"approval_{index}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        broker.register(run_id, approval_id, future)

        assert broker.pending_count() == 1
        assert broker.resolve(run_id, approval_id, "approve") is True
        assert await future == "approve"
        broker.remove(run_id, approval_id)
        assert broker.pending_count() == 0


@pytest.mark.asyncio
async def test_broker_deny_run_fails_closed_for_all_pending_actions():
    broker = RunApprovalBroker()
    futures = []
    for approval_id in ("a", "b", "c"):
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        broker.register("run", approval_id, future)
        futures.append(future)

    assert broker.deny_run("run") == 3
    assert [await future for future in futures] == ["deny", "deny", "deny"]
    assert broker.pending_count("run") == 3


@pytest.mark.asyncio
async def test_broker_rejects_cross_run_and_stale_approval_responses():
    broker = RunApprovalBroker()
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    broker.register("owner", "approval", future)

    assert broker.resolve("other", "approval", "approve") is False
    assert not future.done()
    assert broker.resolve("owner", "missing", "approve") is False
    assert not future.done()
    assert broker.resolve("owner", "approval", "approve") is True
    assert await future == "approve"
    assert broker.resolve("owner", "approval", "approve") is False
