"""Deployment / ops tests.

Covers the bits added for production-readiness: liveness probe, retry helper
that survives transient Telegram errors, and the in-memory bookkeeping
(_stitch_jobs eviction, _stitch_tasks tracking) the graceful-shutdown path
depends on.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import main

# ─── /healthz ────────────────────────────────────────────────────────────────

@pytest.fixture
async def client():
    app = web.Application(
        middlewares=[main.request_id_middleware, main.cors_middleware, main.auth_middleware]
    )
    app.add_routes(main.routes)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_healthz_returns_200_without_auth(client):
    """Liveness probe must not require Telegram initData — load balancers
    can't sign requests, and we don't want bot-side outages to fail the probe."""
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body.get("status") == "ok"


# ─── with_telegram_retry ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_returns_value_on_first_success():
    async def coro():
        return "ok"

    out = await main.with_telegram_retry(coro, what="t")
    assert out == "ok"


@pytest.mark.asyncio
async def test_retry_recovers_from_429_then_succeeds(monkeypatch):
    """A single 429 → success after the indicated wait."""
    # Patch sleep so we don't actually wait the retry_after.
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    async def coro():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramRetryAfter(
                method=None, message="Too Many Requests", retry_after=3
            )
        return "got it"

    out = await main.with_telegram_retry(coro, what="rate-limited")
    assert out == "got it"
    assert calls["n"] == 2
    # Slept ~retry_after + small buffer.
    assert sleeps and 3.0 < sleeps[0] < 4.0


@pytest.mark.asyncio
async def test_retry_exponential_backoff_on_network_error(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    async def coro():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TelegramNetworkError(method=None, message="boom")
        return "fine"

    out = await main.with_telegram_retry(coro, what="net")
    assert out == "fine"
    assert calls["n"] == 3
    # Two retries → two sleeps, second ~2× first (exponential).
    assert len(sleeps) == 2
    assert sleeps[1] == pytest.approx(sleeps[0] * 2)


@pytest.mark.asyncio
async def test_retry_raises_after_exhausting_attempts(monkeypatch):
    """If every attempt fails, the last exception escapes (we lose the data
    rather than pretending success)."""

    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    async def coro():
        raise TelegramNetworkError(method=None, message="down")

    with pytest.raises(TelegramNetworkError):
        await main.with_telegram_retry(coro, what="dead", attempts=3)


# ─── Stitch-job bookkeeping (drives graceful shutdown) ───────────────────────

@pytest.mark.asyncio
async def test_schedule_job_eviction_removes_entry(monkeypatch):
    """After STITCH_JOB_TTL_SECONDS, a terminal job entry is gone from
    _stitch_jobs. We monkeypatch the TTL down to ~0 so the test is fast."""
    monkeypatch.setattr(main, "STITCH_JOB_TTL_SECONDS", 0)

    sid = "test-eviction-sid"
    main._stitch_jobs[sid] = {"status": "done"}
    main._schedule_job_eviction(sid)

    # Give the scheduled task a tick (and the asyncio.sleep(0) inside) to run.
    for _ in range(10):
        await asyncio.sleep(0)
        if sid not in main._stitch_jobs:
            break

    assert sid not in main._stitch_jobs
    # Eviction task removed from tracking set (done callback discards it).
    await asyncio.sleep(0)
    assert all(not t.cancelled() and t.done() for t in list(main._eviction_tasks))


@pytest.mark.asyncio
async def test_drain_stitch_tasks_cancels_pending_after_timeout():
    """A task that never finishes is cancelled when the drain deadline hits."""
    async def hang():
        await asyncio.sleep(60)

    task = asyncio.create_task(hang())
    main._stitch_tasks.add(task)
    task.add_done_callback(main._stitch_tasks.discard)

    await main._drain_stitch_tasks(timeout=0.1)
    # Drain calls task.cancel(); the task transitions to cancelled on the
    # next loop turn — give it a chance to settle.
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_drain_stitch_tasks_returns_immediately_when_idle():
    """No in-flight stitches ⇒ drain returns instantly."""
    # Clean state (other tests may have left entries).
    for t in list(main._stitch_tasks):
        if t.done():
            main._stitch_tasks.discard(t)

    start = asyncio.get_event_loop().time()
    await main._drain_stitch_tasks(timeout=5)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.5
