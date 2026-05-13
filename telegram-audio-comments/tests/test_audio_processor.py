"""Audio stitching tests.

Builds tiny synthetic fixtures (silent OGG original + sine-tone comments) on
disk, runs the real pydub pipeline, and checks the output duration math + the
marker invariants we rely on for the listen-mode skip-to-comment behavior.

These tests require ffmpeg on PATH.
"""
from __future__ import annotations

import shutil

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

import audio_processor
import store

ffmpeg_missing = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is required for stitching tests",
)


def _write_silent_original(filename: str, duration_ms: int = 10_000) -> None:
    seg = AudioSegment.silent(duration=duration_ms, frame_rate=24_000)
    seg.export(str(store.audio_path(filename)), format="ogg", codec="libopus")


def _write_comment(filename: str, duration_ms: int = 1_000) -> None:
    # A 440Hz tone is easier to inspect than silence if a test ever needs to
    # play the output; for now it just has to be valid audio of known length.
    seg = Sine(440).to_audio_segment(duration=duration_ms).set_frame_rate(24_000).set_channels(1)
    # WebM is what MediaRecorder produces in the real Mini App; we store it as
    # .webm but pydub/ffmpeg will sniff the container on load.
    seg.export(str(store.audio_path(filename)), format="webm", codec="libopus")


@ffmpeg_missing
@pytest.mark.asyncio
async def test_stitch_duration_and_marker_invariants():
    """Stitch three comments into a 10s original and check the output shape.

    Specifically:
      * Each marker has end_ms > start_ms.
      * Markers are strictly monotonically increasing in start_ms.
      * Final duration ≈ original_duration + sum(insert_block_durations).
        The "≈" is needed because pydub's OGG/Opus reload has minor rounding
        (a few ms), so we allow ±50ms slack.
    """
    session = store.create_session(
        user_id=1,
        chat_id=1,
        original_audio="orig.ogg",
        original_duration_ms=10_000,
    )
    sid = session["id"]
    _write_silent_original("orig.ogg", duration_ms=10_000)

    timestamps = [2_000, 5_000, 8_000]
    for i, ts in enumerate(timestamps):
        fname = f"rec_{i}.webm"
        _write_comment(fname, duration_ms=1_000)
        await store.add_recording(sid, ts, fname, 1_000)

    session = store.get_session(sid)
    result = audio_processor.stitch(session)

    markers = result["result_markers"]
    assert len(markers) == 3

    # Each insert block has nonzero duration.
    for m in markers:
        assert m["end_ms"] > m["start_ms"], m
        assert m["duration_ms"] == m["end_ms"] - m["start_ms"]

    # Strictly monotonic.
    starts = [m["start_ms"] for m in markers]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)

    insert_total = sum(m["duration_ms"] for m in markers)
    expected = 10_000 + insert_total
    actual = result["result_duration_ms"]
    # Allow small slack from OGG/Opus frame rounding.
    assert abs(actual - expected) <= 50, (actual, expected, insert_total)


@ffmpeg_missing
@pytest.mark.asyncio
async def test_stitch_no_recordings_copies_original():
    """When there are no comments, the result is the original duration."""
    session = store.create_session(
        user_id=1,
        chat_id=1,
        original_audio="orig.ogg",
        original_duration_ms=5_000,
    )
    _write_silent_original("orig.ogg", duration_ms=5_000)

    result = audio_processor.stitch(session)
    assert result["result_markers"] == []
    assert abs(result["result_duration_ms"] - 5_000) <= 50


@ffmpeg_missing
@pytest.mark.asyncio
async def test_stitch_progress_callback_monotonic():
    """progress_cb is invoked with values in [0,1] that never decrease."""
    session = store.create_session(
        user_id=1,
        chat_id=1,
        original_audio="orig.ogg",
        original_duration_ms=6_000,
    )
    sid = session["id"]
    _write_silent_original("orig.ogg", duration_ms=6_000)
    for i, ts in enumerate([1_000, 3_000, 5_000]):
        _write_comment(f"r{i}.webm", duration_ms=500)
        await store.add_recording(sid, ts, f"r{i}.webm", 500)

    session = store.get_session(sid)
    seen: list[float] = []
    audio_processor.stitch(session, progress_cb=seen.append)

    assert seen, "progress_cb was never called"
    assert all(0.0 <= p <= 1.0 for p in seen)
    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0)


@ffmpeg_missing
@pytest.mark.asyncio
async def test_stitch_callback_errors_swallowed():
    """A misbehaving callback must not break stitching."""
    session = store.create_session(
        user_id=1,
        chat_id=1,
        original_audio="orig.ogg",
        original_duration_ms=3_000,
    )
    sid = session["id"]
    _write_silent_original("orig.ogg", duration_ms=3_000)
    _write_comment("r.webm", duration_ms=500)
    await store.add_recording(sid, 1_000, "r.webm", 500)

    def boom(_p):
        raise RuntimeError("boom")

    session = store.get_session(sid)
    # Should not raise.
    result = audio_processor.stitch(session, progress_cb=boom)
    assert result["result_markers"]
