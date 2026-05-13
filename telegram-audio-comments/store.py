"""
Simple file-based session storage for audio comment sessions.
Each session is stored as a JSON file in data/sessions/.
"""

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(_ROOT / "data")))
AUDIO_DIR = DATA_DIR / "audio"
SESSIONS_DIR = DATA_DIR / "sessions"
FILE_UNIQUE_ID_INDEX = SESSIONS_DIR / "_by_file_unique_id.json"


def _ensure_dirs():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


_ensure_dirs()


_session_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_index_lock = asyncio.Lock()


async def _lock_for(session_id: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


def new_id() -> str:
    # 16 bytes → ~22 URL-safe chars, 128 bits of entropy.
    # Existing sessions with 12-hex-char IDs continue to work — change is forward-only.
    return secrets.token_urlsafe(16)


def audio_path(filename: str) -> Path:
    return AUDIO_DIR / filename


def create_session(
    user_id: int,
    chat_id: int,
    original_audio: str,
    original_duration_ms: int = 0,
    parent_session: str | None = None,
    parent_markers: list | None = None,
) -> dict:
    session_id = new_id()
    session = {
        "id": session_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "original_audio": original_audio,       # filename in data/audio/
        "original_duration_ms": original_duration_ms,
        "recordings": [],                         # [{timestamp_ms, filename, duration_ms}]
        "result_audio": None,                     # filename after stitching
        "result_markers": None,                   # [{original_ts, start_ms, end_ms}]
        "parent_session": parent_session,
        "parent_markers": parent_markers or [],   # markers inherited from parent
        "viewers": [],                            # user_ids granted read-only access (listen mode)
        "status": "recording",                    # recording | stitched | sent
        "created_at": datetime.now(UTC).isoformat(),
    }
    _save(session)
    return session


def get_session(session_id: str) -> dict | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


async def update_session(session_id: str, **updates) -> dict | None:
    lock = await _lock_for(session_id)
    async with lock:
        session = get_session(session_id)
        if not session:
            return None
        session.update(updates)
        _save(session)
        return session


async def add_recording(session_id: str, timestamp_ms: int, filename: str, duration_ms: int) -> dict | None:
    lock = await _lock_for(session_id)
    async with lock:
        session = get_session(session_id)
        if not session:
            return None
        session["recordings"].append({
            "timestamp_ms": timestamp_ms,
            "filename": filename,
            "duration_ms": duration_ms,
        })
        session["recordings"].sort(key=lambda r: r["timestamp_ms"])
        _save(session)
        return session


async def update_recording_transcript(
    session_id: str, filename: str, transcript: str
) -> dict | None:
    """Attach a transcript string to the recording with `filename`.

    Looked up by filename rather than index because the index can shift if the
    user deletes a recording while transcription is still running in the
    background. Returns the updated session or None if not found.
    """
    lock = await _lock_for(session_id)
    async with lock:
        session = get_session(session_id)
        if not session:
            return None
        target = None
        for rec in session.get("recordings", []):
            if rec.get("filename") == filename:
                target = rec
                break
        if target is None:
            return None
        target["transcript"] = transcript
        _save(session)
        return session


async def remove_recording(session_id: str, index: int) -> dict | None:
    lock = await _lock_for(session_id)
    async with lock:
        session = get_session(session_id)
        if not session or index < 0 or index >= len(session["recordings"]):
            return None
        rec = session["recordings"].pop(index)
        filepath = audio_path(rec["filename"])
        if filepath.exists():
            try:
                filepath.unlink()
            except OSError:
                pass
        _save(session)
        return session


def _save(session: dict):
    # Atomic write: write to tmp then rename, so concurrent readers never see torn data.
    path = SESSIONS_DIR / f"{session['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(session, f, indent=2)
    os.replace(tmp, path)


# ─── file_unique_id index (lookup stitched results by their Telegram file ID) ──

def _load_index() -> dict:
    if not FILE_UNIQUE_ID_INDEX.exists():
        return {}
    try:
        with open(FILE_UNIQUE_ID_INDEX) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


async def set_file_unique_id(file_unique_id: str, session_id: str) -> None:
    async with _index_lock:
        idx = _load_index()
        idx[file_unique_id] = session_id
        tmp = FILE_UNIQUE_ID_INDEX.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(idx, f)
        os.replace(tmp, FILE_UNIQUE_ID_INDEX)


def get_session_by_file_unique_id(file_unique_id: str) -> dict | None:
    sid = _load_index().get(file_unique_id)
    if not sid:
        return None
    return get_session(sid)


# ─── Maintenance: enumerate / delete sessions (used by background cleanup) ─────

def list_session_ids() -> list[str]:
    """Return session IDs for every session JSON in SESSIONS_DIR (skipping the
    file_unique_id index)."""
    if not SESSIONS_DIR.exists():
        return []
    ids = []
    for path in SESSIONS_DIR.glob("*.json"):
        if path.name.startswith("_"):
            continue
        ids.append(path.stem)
    return ids


async def delete_session(session_id: str) -> bool:
    """Remove a session's JSON, all referenced audio files, and any index
    entries pointing to it. Idempotent; returns True if anything was deleted."""
    lock = await _lock_for(session_id)
    async with lock:
        session = get_session(session_id)
        if not session:
            return False

        audio_files: set[str] = set()
        for key in ("original_audio", "original_audio_web", "result_audio", "result_audio_web"):
            name = session.get(key)
            if name:
                audio_files.add(name)
        for rec in session.get("recordings") or []:
            name = rec.get("filename")
            if name:
                audio_files.add(name)

        for name in audio_files:
            p = audio_path(name)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        path = SESSIONS_DIR / f"{session_id}.json"
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    # Index entries map file_unique_id -> session_id; strip ours.
    async with _index_lock:
        idx = _load_index()
        stale = [fuid for fuid, sid in idx.items() if sid == session_id]
        if stale:
            for fuid in stale:
                idx.pop(fuid, None)
            tmp = FILE_UNIQUE_ID_INDEX.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(idx, f)
            os.replace(tmp, FILE_UNIQUE_ID_INDEX)

    # Drop the lock entry too so memory doesn't grow forever.
    async with _locks_guard:
        _session_locks.pop(session_id, None)
    return True
