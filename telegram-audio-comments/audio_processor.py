"""
Audio processing: stitching recordings into original audio at specified timestamps.
Uses pydub (requires ffmpeg system dependency).
"""

from pydub import AudioSegment
from pydub.generators import Sine

import store


# Generate indicator tones (created once, reused)
def _make_beep(freq=880, duration_ms=80, gain=-18):
    tone = Sine(freq).to_audio_segment(duration=duration_ms)
    # Apply fade to avoid clicks
    return tone.apply_gain(gain).fade_in(10).fade_out(10)


# Double beep = "comment starts", single beep = "comment ends"
_beep = _make_beep(880, 80, -18)
_silence_gap = AudioSegment.silent(duration=40)
_insert_start_sound = _beep + _silence_gap + _beep  # ~200ms double beep
_insert_end_sound = _beep                             # ~80ms single beep
_separator = AudioSegment.silent(duration=120)         # brief silence padding


def load_audio(filename: str) -> AudioSegment:
    """Load an audio file from the data/audio directory."""
    path = store.audio_path(filename)
    return AudioSegment.from_file(str(path))


def stitch(session: dict, progress_cb=None) -> dict:
    """
    Stitch all recordings into the original audio for the given session.

    Pure-ish compute: reads/writes audio files, but does NOT touch the session JSON.
    Returns a dict of fields the caller should persist via store.update_session.
    Runs synchronously and is safe to call inside asyncio.to_thread.

    progress_cb, if given, is called with a float in [0, 1] after each
    recording is appended and again at the end after export. Errors raised by
    the callback are swallowed so a misbehaving caller can't break stitching.
    """
    def _emit(p: float):
        if progress_cb is None:
            return
        try:
            progress_cb(max(0.0, min(1.0, p)))
        except Exception:
            pass

    session_id = session["id"]
    original = load_audio(session["original_audio"])
    recordings = session["recordings"]

    if not recordings:
        # No recordings — just copy original as result
        result_filename = f"result_{session_id}.ogg"
        result_path = store.audio_path(result_filename)
        original.export(str(result_path), format="ogg", codec="libopus")
        _emit(1.0)
        return {
            "result_audio": result_filename,
            "result_audio_web": None,
            "result_markers": [],
            "result_duration_ms": len(original),
            "status": "stitched",
        }

    # Sort recordings by timestamp (should already be sorted, but be safe)
    sorted_recs = sorted(recordings, key=lambda r: r["timestamp_ms"])
    total = len(sorted_recs)
    # Reserve the last ~10% for the final export step, which can be slow for
    # long results. The per-recording loop fills the first 90%.
    loop_share = 0.9

    result = AudioSegment.empty()
    markers = []
    last_pos = 0       # position in original audio (ms)

    for i, rec in enumerate(sorted_recs):
        ts = rec["timestamp_ms"]
        rec_audio = load_audio(rec["filename"])

        # Normalize recording volume to roughly match original
        orig_loudness = original.dBFS
        if rec_audio.dBFS != float('-inf') and orig_loudness != float('-inf'):
            volume_diff = orig_loudness - rec_audio.dBFS
            rec_audio = rec_audio.apply_gain(volume_diff)

        # Make recording mono if original is mono (consistency)
        if original.channels == 1 and rec_audio.channels > 1:
            rec_audio = rec_audio.set_channels(1)
        elif original.channels > 1 and rec_audio.channels == 1:
            rec_audio = rec_audio.set_channels(original.channels)

        # Match sample rate
        if rec_audio.frame_rate != original.frame_rate:
            rec_audio = rec_audio.set_frame_rate(original.frame_rate)

        # Append original audio up to this timestamp
        segment = original[last_pos:ts]
        result += segment

        # Build the insert block: [separator + start_beep + separator + recording + separator + end_beep + separator]
        insert_block = _separator + _insert_start_sound + _separator + rec_audio + _separator + _insert_end_sound + _separator

        # Ensure insert block matches audio properties
        insert_block = insert_block.set_channels(original.channels).set_frame_rate(original.frame_rate)

        insert_start_ms = len(result)
        result += insert_block
        insert_end_ms = len(result)

        markers.append({
            "original_timestamp_ms": ts,
            "start_ms": insert_start_ms,
            "end_ms": insert_end_ms,
            "duration_ms": insert_end_ms - insert_start_ms,
            "type": "comment",
            # Snapshot the per-recording transcript at stitch time so the
            # listen-mode UI and the chat-caption chapter list don't have to
            # cross-reference the recordings list (which gets pruned).
            "transcript": rec.get("transcript"),
        })

        last_pos = ts
        _emit((i + 1) / total * loop_share)

    # Append remaining original audio
    result += original[last_pos:]

    # Export result as OGG (for Telegram) and MP3 (for web playback)
    result_ogg = f"result_{session_id}.ogg"
    result_mp3 = f"result_{session_id}.mp3"

    result.export(str(store.audio_path(result_ogg)), format="ogg", codec="libopus")
    # -q:a 6 (~96 kbps VBR) keeps a 40-min stitched result under the
    # Telegram Bot API ~50 MB outbound cap.
    result.export(str(store.audio_path(result_mp3)), format="mp3", parameters=["-q:a", "6"])
    _emit(1.0)

    return {
        "result_audio": result_ogg,
        "result_audio_web": result_mp3,
        "result_markers": markers,
        "result_duration_ms": len(result),
        "status": "stitched",
    }


def convert_to_mp3(input_filename: str, output_filename: str) -> str:
    """Convert any audio file to MP3 for web playback."""
    audio = load_audio(input_filename)
    out_path = store.audio_path(output_filename)
    audio.export(str(out_path), format="mp3", parameters=["-q:a", "6"])
    return output_filename


def get_duration_ms(filename: str) -> int:
    """Get duration of an audio file in milliseconds."""
    audio = load_audio(filename)
    return len(audio)
