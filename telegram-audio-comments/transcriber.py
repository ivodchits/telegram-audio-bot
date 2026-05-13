"""
Per-comment speech-to-text using faster-whisper.

The model is loaded lazily on first use — cold start is a few seconds for the
`base` model on CPU, but a comment-flow user uploads recordings in series so
the cost amortizes. Each transcription runs inside `asyncio.to_thread` so the
event loop stays responsive.

faster-whisper is an *optional* runtime dependency. If the package isn't
installed (or the model fails to load), transcription is silently disabled and
recordings simply stay without a `transcript` field. This lets deployments
opt out by not installing the wheel — no env flag flip required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import store

logger = logging.getLogger(__name__)

ENABLED = os.getenv("TRANSCRIBE_ENABLED", "true").lower() in ("1", "true", "yes")
MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
# Optional ISO-639-1 hint (e.g. "en", "ru"). When unset, Whisper auto-detects
# per-clip — fine for short comments but burns ~30% extra time.
LANGUAGE = os.getenv("WHISPER_LANGUAGE") or None

# Bound transcription concurrency so simultaneous uploads don't blow CPU.
# faster-whisper itself isn't thread-safe across .transcribe() calls on CTranslate2
# CPU backends without care, and serializing is plenty fast for short clips.
_MAX_PARALLEL = int(os.getenv("WHISPER_MAX_PARALLEL", "1"))
_semaphore: asyncio.Semaphore | None = None

_model = None
_model_lock = threading.Lock()
_load_failed = False


def is_available() -> bool:
    """Whether transcription is configured and the model loaded (or loadable)."""
    return ENABLED and not _load_failed


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, _MAX_PARALLEL))
    return _semaphore


def _load_model():
    """Import faster-whisper and load the model. Double-checked locking — many
    upload handlers may race the first transcription."""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _model_lock:
        if _model is not None or _load_failed:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            logger.warning(f"faster-whisper not installed; transcription disabled ({e})")
            _load_failed = True
            return None
        try:
            logger.info(
                f"Loading Whisper model '{MODEL_NAME}' on {DEVICE}/{COMPUTE_TYPE}..."
            )
            _model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
            logger.info("Whisper model ready.")
        except Exception as e:
            logger.warning(f"Failed to load Whisper model: {e}")
            _load_failed = True
            return None
        return _model


def _transcribe_sync(filename: str) -> str | None:
    model = _load_model()
    if model is None:
        return None
    path = store.audio_path(filename)
    if not path.exists():
        return None
    try:
        segments, _info = model.transcribe(
            str(path),
            language=LANGUAGE,
            beam_size=1,
            vad_filter=True,
        )
        text = " ".join((seg.text or "").strip() for seg in segments).strip()
        return text
    except Exception as e:
        logger.warning(f"Transcription failed for {filename}: {e}")
        return None


async def transcribe(filename: str) -> str | None:
    """Transcribe a single audio file. Returns text on success, None on failure
    or when the feature is disabled."""
    if not ENABLED:
        return None
    sem = _get_semaphore()
    async with sem:
        return await asyncio.to_thread(_transcribe_sync, filename)


async def transcribe_recording(session_id: str, filename: str) -> None:
    """Background entry point: transcribe a recording and persist the result
    onto the session JSON. Safe to schedule via asyncio.create_task — all
    exceptions are caught and logged."""
    if not ENABLED:
        return
    try:
        text = await transcribe(filename)
    except Exception as e:
        logger.warning(f"transcribe_recording {session_id}/{filename}: {e}")
        return
    if text is None:
        return
    try:
        await store.update_recording_transcript(session_id, filename, text)
    except Exception as e:
        logger.warning(f"persist transcript {session_id}/{filename}: {e}")
