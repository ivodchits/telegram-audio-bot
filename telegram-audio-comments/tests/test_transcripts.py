"""Transcript-related tests.

The Whisper model itself is heavy and optional, so these tests cover the
moving parts that don't need it: the session-store update, the caption
builder that packs chapter markers into Telegram's 1024-char cap, and the
guard that skips transcription when disabled.
"""
from __future__ import annotations

import asyncio

import pytest

import main
import store
import transcriber


def _make_session():
    return store.create_session(
        user_id=1,
        chat_id=1,
        original_audio="orig.ogg",
        original_duration_ms=10_000,
    )


# ─── store.update_recording_transcript ───────────────────────────────────────

@pytest.mark.asyncio
async def test_update_recording_transcript_writes_field():
    s = _make_session()
    sid = s["id"]
    await store.add_recording(sid, 1000, "rec_a.webm", 500)

    result = await store.update_recording_transcript(sid, "rec_a.webm", "hello world")
    assert result is not None
    assert result["recordings"][0]["transcript"] == "hello world"

    # Persisted on disk too.
    reloaded = store.get_session(sid)
    assert reloaded["recordings"][0]["transcript"] == "hello world"


@pytest.mark.asyncio
async def test_update_recording_transcript_matches_by_filename_not_index():
    """A delete that shifts indices must not misroute the transcript write."""
    s = _make_session()
    sid = s["id"]
    await store.add_recording(sid, 1000, "rec_a.webm", 500)
    await store.add_recording(sid, 2000, "rec_b.webm", 500)
    await store.add_recording(sid, 3000, "rec_c.webm", 500)

    # Delete the middle one — now rec_c is at index 1, not 2.
    await store.remove_recording(sid, 1)

    await store.update_recording_transcript(sid, "rec_c.webm", "third clip")
    reloaded = store.get_session(sid)
    transcripts = {r["filename"]: r.get("transcript") for r in reloaded["recordings"]}
    assert transcripts["rec_c.webm"] == "third clip"
    assert transcripts["rec_a.webm"] is None


@pytest.mark.asyncio
async def test_update_recording_transcript_unknown_filename_returns_none():
    s = _make_session()
    sid = s["id"]
    await store.add_recording(sid, 1000, "rec_a.webm", 500)
    assert await store.update_recording_transcript(sid, "ghost.webm", "x") is None


# ─── transcriber.transcribe_recording when disabled ──────────────────────────

@pytest.mark.asyncio
async def test_transcribe_recording_no_op_when_disabled(monkeypatch):
    """conftest sets TRANSCRIBE_ENABLED=false; transcribe_recording must return
    immediately and not touch the session."""
    monkeypatch.setattr(transcriber, "ENABLED", False)

    s = _make_session()
    sid = s["id"]
    await store.add_recording(sid, 1000, "rec.webm", 500)

    await transcriber.transcribe_recording(sid, "rec.webm")
    reloaded = store.get_session(sid)
    assert "transcript" not in reloaded["recordings"][0]


@pytest.mark.asyncio
async def test_is_available_false_when_disabled(monkeypatch):
    monkeypatch.setattr(transcriber, "ENABLED", False)
    assert transcriber.is_available() is False


# ─── main._build_caption_with_chapters ───────────────────────────────────────

def test_caption_no_markers_just_header_and_acid():
    cap, overflow = main._build_caption_with_chapters(
        "🎙️ Audio with 0 comment(s) • 0:30",
        [],
        "📎 ACID:abc123",
        main.TELEGRAM_CAPTION_LIMIT,
    )
    assert overflow is None
    assert "ACID:abc123" in cap
    assert "🎙️" in cap


def test_caption_with_transcripts_inlines_chapters():
    markers = [
        {"start_ms": 12_000, "transcript": "test one two three"},
        {"start_ms": 105_000, "transcript": "and the answer is forty two"},
    ]
    cap, overflow = main._build_caption_with_chapters(
        "🎙️ Audio with 2 comment(s) • 2:30",
        markers,
        "📎 ACID:sid",
        main.TELEGRAM_CAPTION_LIMIT,
    )
    assert overflow is None
    assert "1. 0:12" in cap
    assert "test one two three" in cap
    assert "2. 1:45" in cap
    assert "ACID:sid" in cap


def test_caption_no_transcript_yields_timestamp_only_line():
    markers = [{"start_ms": 5_000, "transcript": None}]
    cap, _ = main._build_caption_with_chapters(
        "header", markers, "📎 ACID:s", main.TELEGRAM_CAPTION_LIMIT
    )
    # Line still numbers the chapter but skips the quoted text.
    assert "1. 0:05" in cap
    assert '"' not in cap  # no empty quotes


def test_caption_overflow_keeps_acid_and_returns_chapter_list():
    """When the chapter list won't fit, the caption keeps just header + ACID
    (so forward-back detection still works) and the full list spills out."""
    # 80-char long transcript × many comments quickly exceeds 1024.
    long_text = "x" * 80
    markers = [
        {"start_ms": i * 1000, "transcript": long_text} for i in range(20)
    ]
    cap, overflow = main._build_caption_with_chapters(
        "🎙️ Audio with 20 comment(s) • 0:20",
        markers,
        "📎 ACID:sid",
        main.TELEGRAM_CAPTION_LIMIT,
    )
    assert overflow is not None
    assert len(cap) <= main.TELEGRAM_CAPTION_LIMIT
    assert "ACID:sid" in cap
    # Chapter content moved to overflow.
    assert "20. " in overflow
    assert "1. " in overflow


def test_caption_truncates_very_long_per_line_text():
    """Single huge transcript line gets truncated, not dropped."""
    markers = [{"start_ms": 1000, "transcript": "y" * 500}]
    cap, _ = main._build_caption_with_chapters(
        "header", markers, "📎 ACID:s", main.TELEGRAM_CAPTION_LIMIT
    )
    # Truncated and an ellipsis appended somewhere.
    assert "…" in cap
    # Line must not exceed our per-line cap + framing.
    for line in cap.split("\n"):
        if line.startswith("1."):
            assert len(line) < 120


# ─── main._wait_for_transcripts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_for_transcripts_returns_immediately_when_all_present():
    s = _make_session()
    sid = s["id"]
    await store.add_recording(sid, 1000, "r.webm", 500)
    await store.update_recording_transcript(sid, "r.webm", "done")

    start = asyncio.get_event_loop().time()
    sess = await main._wait_for_transcripts(sid, timeout_s=5)
    elapsed = asyncio.get_event_loop().time() - start

    assert sess["recordings"][0]["transcript"] == "done"
    # Should return on the first poll, well under the timeout.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_wait_for_transcripts_times_out_with_partial_state():
    s = _make_session()
    sid = s["id"]
    await store.add_recording(sid, 1000, "r.webm", 500)
    # Never set a transcript — wait should give up at the deadline.

    sess = await main._wait_for_transcripts(sid, timeout_s=1)
    # Returns whatever's there; transcript still missing.
    assert sess["recordings"][0].get("transcript") is None
