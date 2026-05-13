"""Session store tests.

Covers the locking added in DEVELOPMENT_PLAN step 1.4: concurrent adds must all
land. Also confirms remove_recording deletes the on-disk audio file.
"""
from __future__ import annotations

import asyncio

import pytest

import store


def _make_session():
    return store.create_session(
        user_id=42,
        chat_id=100,
        original_audio="orig.ogg",
        original_duration_ms=10_000,
    )


@pytest.mark.asyncio
async def test_concurrent_add_recording_all_land():
    """Five simultaneous add_recording calls all end up in the session JSON.

    Without the per-session asyncio.Lock this races on the read-modify-write of
    the JSON file and writes get lost. We don't expect any specific final
    ordering of timestamps, only that every entry shows up.
    """
    session = _make_session()
    sid = session["id"]

    async def add(i: int):
        # File doesn't need to exist for add_recording (it only writes JSON).
        await store.add_recording(sid, timestamp_ms=i * 1000, filename=f"r{i}.webm", duration_ms=500)

    await asyncio.gather(*(add(i) for i in range(5)))

    reloaded = store.get_session(sid)
    assert reloaded is not None
    filenames = {r["filename"] for r in reloaded["recordings"]}
    assert filenames == {f"r{i}.webm" for i in range(5)}
    # Stored sorted by timestamp_ms.
    timestamps = [r["timestamp_ms"] for r in reloaded["recordings"]]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_remove_recording_deletes_file():
    """remove_recording must unlink the recording's audio file on disk."""
    session = _make_session()
    sid = session["id"]

    # Drop a real file so the unlink path runs.
    fname = "rec_to_delete.webm"
    path = store.audio_path(fname)
    path.write_bytes(b"fake-audio-data")
    assert path.exists()

    await store.add_recording(sid, timestamp_ms=1000, filename=fname, duration_ms=500)
    reloaded = store.get_session(sid)
    assert len(reloaded["recordings"]) == 1

    result = await store.remove_recording(sid, 0)
    assert result is not None
    assert result["recordings"] == []
    assert not path.exists(), "audio file should be deleted from disk"


@pytest.mark.asyncio
async def test_remove_recording_out_of_range_returns_none():
    session = _make_session()
    sid = session["id"]
    assert await store.remove_recording(sid, 0) is None
    assert await store.remove_recording(sid, -1) is None


@pytest.mark.asyncio
async def test_file_unique_id_index_roundtrip():
    """set_file_unique_id should make get_session_by_file_unique_id return the same session."""
    session = _make_session()
    sid = session["id"]
    fuid = "AgADBAADdLcxG"

    await store.set_file_unique_id(fuid, sid)
    found = store.get_session_by_file_unique_id(fuid)
    assert found is not None
    assert found["id"] == sid


@pytest.mark.asyncio
async def test_delete_session_removes_files_and_index():
    """delete_session should unlink referenced audio files and drop index entries."""
    session = _make_session()
    sid = session["id"]

    # Drop the original audio file referenced by the session, plus a recording.
    orig = store.audio_path("orig.ogg")
    orig.write_bytes(b"original")
    rec = store.audio_path("rec.webm")
    rec.write_bytes(b"comment")

    await store.add_recording(sid, 1000, "rec.webm", 500)
    await store.set_file_unique_id("FUID_X", sid)

    assert await store.delete_session(sid)
    assert not orig.exists()
    assert not rec.exists()
    assert store.get_session(sid) is None
    assert store.get_session_by_file_unique_id("FUID_X") is None
