"""API auth tests.

Exercises `auth_middleware` with the aiohttp test client. The conftest sets
`DEV_MODE=true` so the other test modules can hit endpoints without signing;
this module monkeypatches it back to False so the real verifier runs.

We build a valid Telegram `initData` string by HMACing with the same BOT_TOKEN
the conftest installed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote, urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import main
import store

BOT_TOKEN = os.environ["BOT_TOKEN"]


def make_init_data(user_id: int, age_seconds: int = 0) -> str:
    """Build a valid Telegram WebApp initData query string for `user_id`.

    Mirrors what Telegram clients produce: alphabetically sorted key=value
    lines joined by \n, HMAC-SHA256 with key = HMAC(BOT_TOKEN, "WebAppData").
    """
    auth_date = str(int(time.time()) - age_seconds)
    user = json.dumps({"id": user_id, "first_name": "T"}, separators=(",", ":"))
    fields = {"auth_date": auth_date, "user": user}
    dcs = "\n".join(sorted(f"{k}={v}" for k, v in fields.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    fields["hash"] = sig
    return urlencode(fields)


@pytest.fixture
def auth_on(monkeypatch):
    """Turn off DEV_MODE so auth_middleware actually runs."""
    monkeypatch.setattr(main, "DEV_MODE", False)


@pytest.fixture
async def client(auth_on):
    app = web.Application(
        middlewares=[main.request_id_middleware, main.cors_middleware, main.auth_middleware]
    )
    app.add_routes(main.routes)
    async with TestClient(TestServer(app)) as c:
        yield c


def _make_session(owner_id: int = 1001) -> dict:
    return store.create_session(
        user_id=owner_id,
        chat_id=1,
        original_audio="orig.ogg",
        original_duration_ms=10_000,
    )


@pytest.mark.asyncio
async def test_get_session_no_init_data_returns_401(client):
    """No header, no query param ⇒ 401."""
    s = _make_session()
    resp = await client.get(f"/api/session/{s['id']}")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_session_invalid_hash_returns_401(client):
    """Garbage initData ⇒ 401 (hash mismatch)."""
    s = _make_session()
    resp = await client.get(
        f"/api/session/{s['id']}",
        headers={"X-Telegram-Init-Data": "auth_date=1&user=%7B%22id%22%3A1%7D&hash=deadbeef"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_session_wrong_user_returns_403(client):
    """Valid initData but a different user_id than the session owner ⇒ 403."""
    s = _make_session(owner_id=1001)
    init = make_init_data(user_id=9999)  # not the owner, not a viewer
    resp = await client.get(
        f"/api/session/{s['id']}",
        headers={"X-Telegram-Init-Data": init},
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_get_session_owner_returns_200(client):
    """Owner's valid initData ⇒ 200 and returns the session payload."""
    s = _make_session(owner_id=1001)
    init = make_init_data(user_id=1001)
    resp = await client.get(
        f"/api/session/{s['id']}",
        headers={"X-Telegram-Init-Data": init},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == s["id"]
    assert body["original_duration_ms"] == 10_000


@pytest.mark.asyncio
async def test_get_session_viewer_returns_200(client):
    """A user_id in `viewers` (listen-mode grant) can read but not write."""
    s = _make_session(owner_id=1001)
    await store.update_session(s["id"], viewers=[2002])

    init = make_init_data(user_id=2002)
    resp = await client.get(
        f"/api/session/{s['id']}",
        headers={"X-Telegram-Init-Data": init},
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_delete_recording_viewer_forbidden(client):
    """Read-only viewers cannot mutate the session."""
    s = _make_session(owner_id=1001)
    await store.update_session(s["id"], viewers=[2002])
    await store.add_recording(s["id"], 1000, "fake.webm", 500)

    init = make_init_data(user_id=2002)
    resp = await client.delete(
        f"/api/session/{s['id']}/recording/0",
        headers={"X-Telegram-Init-Data": init},
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_audio_endpoint_accepts_init_data_query_param(client):
    """<audio src=...?tgInitData=...> can't send headers; the query path works."""
    s = _make_session(owner_id=1001)
    init = make_init_data(user_id=1001)
    # 404 is fine (no audio file on disk); the point is we got past auth.
    # A 401/403 would mean auth_middleware rejected us. Note `quote(init)` —
    # initData contains '&' separators that must be escaped when nested inside
    # the outer query string (this mirrors `encodeURIComponent` in the JS).
    resp = await client.get(
        f"/api/audio/{s['id']}/original?tgInitData={quote(init)}",
    )
    assert resp.status != 401
    assert resp.status != 403


@pytest.mark.asyncio
async def test_stale_init_data_returns_401(client):
    """auth_date too far in the past ⇒ replay-protection trips and returns 401."""
    s = _make_session(owner_id=1001)
    # Default MAX_INIT_DATA_AGE_SECONDS is 86_400 (24h). Two days back is stale.
    init = make_init_data(user_id=1001, age_seconds=2 * 86_400)
    resp = await client.get(
        f"/api/session/{s['id']}",
        headers={"X-Telegram-Init-Data": init},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_recording_not_owner_403(client):
    """The new GET /recording/<index> endpoint is owner-only."""
    s = _make_session(owner_id=1001)
    await store.update_session(s["id"], viewers=[2002])
    await store.add_recording(s["id"], 1000, "fake.webm", 500)

    init = make_init_data(user_id=2002)  # viewer, not owner
    resp = await client.get(
        f"/api/session/{s['id']}/recording/0",
        headers={"X-Telegram-Init-Data": init},
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_app_route_skips_auth(client):
    """/app is the Mini App HTML; Telegram loads it directly and can't sign it."""
    resp = await client.get("/app")
    # Either 200 (file served) or 500 if the path resolved wrong — what we
    # care about here is that auth_middleware did not gate it.
    assert resp.status != 401
    assert resp.status != 403
