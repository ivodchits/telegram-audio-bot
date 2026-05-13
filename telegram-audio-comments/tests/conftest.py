"""Shared test fixtures.

The session-scoped `data_dir` fixture redirects `store.DATA_DIR` (and the audio
/ sessions / index paths derived from it) at a tmp directory so tests never
touch real session data. A function-scoped autouse fixture wipes that directory
between tests so each test starts from a clean slate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the package directory importable regardless of where pytest is invoked.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Bypass auth before main.py is imported — tests don't sign initData.
os.environ.setdefault("DEV_MODE", "true")
# main.py rejects construction if BOT_TOKEN is empty; a syntactically valid
# placeholder is enough since we never actually talk to Telegram.
os.environ.setdefault("BOT_TOKEN", "123456:test-token-for-pytest-only-not-real")
# Transcription loads a multi-MB Whisper model; tests never want that side
# effect (and CI usually doesn't have faster-whisper installed anyway).
os.environ.setdefault("TRANSCRIBE_ENABLED", "false")


@pytest.fixture(scope="session", autouse=True)
def data_dir(tmp_path_factory):
    """Redirect store paths to a per-session tmp directory."""
    import store

    tmp = tmp_path_factory.mktemp("audio-bot-data")
    store.DATA_DIR = tmp
    store.AUDIO_DIR = tmp / "audio"
    store.SESSIONS_DIR = tmp / "sessions"
    store.FILE_UNIQUE_ID_INDEX = store.SESSIONS_DIR / "_by_file_unique_id.json"
    store.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    store.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    yield tmp


@pytest.fixture(autouse=True)
def _clean_data_dir(data_dir):
    """Wipe sessions + audio + index between tests so state never leaks."""
    import store

    for d in (store.SESSIONS_DIR, store.AUDIO_DIR):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            continue
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
    # Also drop any session locks accumulated from prior tests.
    store._session_locks.clear()
    yield
