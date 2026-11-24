"""Adversarial lifecycle coverage for email background work."""

import asyncio

import pytest

from routes import email_pollers


@pytest.fixture(autouse=True)
def _isolated_owner_lifecycle(monkeypatch):
    """Do not inherit the root app's durable owner callbacks or registry."""
    monkeypatch.setattr(email_pollers, "_OWNER_ACTIVITY", {})
    monkeypatch.setattr(email_pollers, "_OWNER_DRAINING", set())
    monkeypatch.setattr(email_pollers, "_OWNER_FENCE_CHECKER", None)
    monkeypatch.setattr(email_pollers, "_OWNER_KNOWN_CHECKER", None)


@pytest.mark.asyncio
async def test_owner_drain_does_not_join_foreign_owner_or_admit_late_work():
    email_pollers._OWNER_ACTIVITY.clear()
    email_pollers._OWNER_DRAINING.clear()
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    a = email_pollers._track_owner_task(work(), "alice")
    b = email_pollers._track_owner_task(work(), "bob")
    await started.wait()
    email_pollers.begin_owner_drain("alice")
    assert email_pollers._track_owner_task(work(), "alice") is None
    waiter = asyncio.create_task(email_pollers.drain_owner_activity("alice"))
    await asyncio.sleep(0)
    assert not waiter.done()
    assert not b.done()
    release.set()
    assert (await waiter)["owner"] == "alice"
    b.cancel()
    await asyncio.gather(a, b, return_exceptions=True)
    email_pollers.end_owner_drain("alice")


@pytest.mark.asyncio
async def test_cancelled_drain_waiter_does_not_cancel_thread_activity():
    email_pollers._OWNER_ACTIVITY.clear()
    email_pollers._OWNER_DRAINING.clear()
    entered = __import__("threading").Event()
    release = __import__("threading").Event()

    def blocking():
        entered.set()
        release.wait()
        return "done"

    task = email_pollers._track_owner_thread(blocking, "alice")
    await asyncio.to_thread(entered.wait, 2)
    waiter = asyncio.create_task(email_pollers.drain_owner_activity("alice"))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not task.done()
    assert email_pollers._OWNER_ACTIVITY.get("alice")
    second = asyncio.create_task(email_pollers.drain_owner_activity("alice"))
    await asyncio.sleep(0.01)
    assert not second.done()
    release.set()
    assert (await second)["owner"] == "alice"


@pytest.mark.asyncio
async def test_direct_pass_rejects_foreign_account(monkeypatch):
    monkeypatch.setattr(email_pollers, "_owner_for_email_account", lambda _aid: "bob")
    result = await email_pollers._auto_summarize_pass_single(
        account_id="acct-bob", owner="alice"
    )
    assert "not owned" in result.lower()


@pytest.mark.asyncio
async def test_direct_pass_resolves_account_owner_before_durable_fence(monkeypatch):
    monkeypatch.setattr(email_pollers, "_owner_for_email_account", lambda _aid: "alice")
    monkeypatch.setattr(email_pollers, "_OWNER_FENCE_CHECKER", lambda owner: owner == "alice")
    monkeypatch.setattr(email_pollers, "_OWNER_KNOWN_CHECKER", lambda _owner: True)

    called = False

    async def unexpected_impl(**_kwargs):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(email_pollers, "_auto_summarize_pass_single_impl", unexpected_impl)
    result = await email_pollers._auto_summarize_pass_single(account_id="acct-alice")

    assert result == "Email owner lifecycle is fenced"
    assert called is False


@pytest.mark.asyncio
async def test_run_flags_do_not_save_global_settings(monkeypatch):
    seen = {}

    async def fake_pass(**kwargs):
        seen.update(kwargs)
        return "Nothing to do"

    monkeypatch.setattr(email_pollers, "_auto_summarize_pass", fake_pass)
    monkeypatch.setattr(email_pollers, "_save_settings", lambda *_args: (_ for _ in ()).throw(AssertionError("global save")))
    result = await email_pollers._run_auto_summarize_once(
        do_summary=True, do_reply=False, owner="alice"
    )
    assert result == "Nothing to do"
    assert seen["owner"] == "alice"
    assert seen["settings_overrides"]["email_auto_reply"] is False
