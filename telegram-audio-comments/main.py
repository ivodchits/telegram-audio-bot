"""
Telegram Audio Comments Bot
============================
Main entry point: runs the Telegram bot (polling) and aiohttp web server concurrently.
The web server serves the Mini App and API endpoints for audio upload/stitching.
"""

import asyncio
import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

import audio_processor
import cleanup
import store
import transcriber

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8081")
PORT = int(os.getenv("PORT", "8081"))
CONTEXT_SECONDS = int(os.getenv("CONTEXT_SECONDS", "5"))

# Auth & CORS
DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
MAX_INIT_DATA_AGE_SECONDS = int(os.getenv("MAX_INIT_DATA_AGE_SECONDS", "86400"))  # 24h

# Resource limits
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "2400"))
MAX_AUDIO_MB = int(os.getenv("MAX_AUDIO_MB", "50"))
MAX_RECORDING_MB = int(os.getenv("MAX_RECORDING_MB", "5"))
LONG_RESULT_THRESHOLD_MS = 10 * 60 * 1000

# Telegram media-caption hard limit. Used when packing the chapter-marker
# transcript list into the stitched result's caption.
TELEGRAM_CAPTION_LIMIT = 1024

# How long the stitch job waits for any still-running per-recording
# transcriptions before snapshotting. Short clips finish in 1-3s on CPU; this
# is a soft ceiling so the user isn't blocked forever on a stuck job.
TRANSCRIBE_WAIT_SECONDS = int(os.getenv("TRANSCRIBE_WAIT_SECONDS", "10"))

# Delivery mode: "polling" (long-poll getUpdates) or "webhook".
MODE = os.getenv("MODE", "polling").lower()
WEBHOOK_SECRET_PATH = os.getenv("WEBHOOK_SECRET_PATH", "")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "")

_parsed_base = urlparse(BASE_URL)
ALLOWED_ORIGIN = (
    f"{_parsed_base.scheme}://{_parsed_base.netloc}"
    if _parsed_base.scheme and _parsed_base.netloc
    else BASE_URL.rstrip("/")
)

# ─── Logging: request_id via ContextVar ────────────────────────────────────────
# Every API call and every Telegram update mints its own request_id; the
# contextvar flows through awaits so all logs from one handler share an ID.
# When no request_id is set (e.g. startup logs), the filter falls back to "-".

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
)
for _h in logging.getLogger().handlers:
    _h.addFilter(_RequestIdFilter())
logger = logging.getLogger(__name__)


def _new_request_id() -> str:
    return secrets.token_hex(4)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


class RequestIdMiddleware(BaseMiddleware):
    """Bind a fresh request_id to the contextvar for the lifetime of one update."""

    async def __call__(self, handler, event, data):
        token = request_id_var.set(_new_request_id())
        try:
            return await handler(event, data)
        finally:
            request_id_var.reset(token)


dp.update.middleware(RequestIdMiddleware())


# ─── Telegram WebApp Auth Verification ─────────────────────────────────────────

def verify_webapp_data(init_data: str) -> dict | None:
    """Verify Telegram WebApp initData and extract user info."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        # Extract hash
        received_hash = parsed.get("hash", [""])[0]
        if not received_hash:
            return None

        # Build data-check-string (alphabetically sorted, excluding hash)
        items = sorted(
            f"{key}={values[0]}"
            for key, values in parsed.items()
            if key != "hash"
        )
        data_check_string = "\n".join(items)

        # Compute expected hash
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, expected_hash):
            return None

        # Replay protection: auth_date must be recent (allow small clock skew).
        try:
            auth_date = int(parsed.get("auth_date", ["0"])[0])
        except (ValueError, TypeError):
            return None
        age = time.time() - auth_date
        if age < -300 or age > MAX_INIT_DATA_AGE_SECONDS:
            return None

        # Parse user JSON
        user_json = parsed.get("user", ["{}"])[0]
        user = json.loads(user_json)
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            return None
        return user
    except Exception as e:
        logger.warning(f"WebApp auth verification failed: {e}")
        return None


# ─── Authorization helpers ─────────────────────────────────────────────────────

def _is_owner(request: web.Request, session: dict) -> bool:
    tg_user = request.get("tg_user")
    if tg_user is None:  # DEV_MODE
        return True
    return tg_user.get("id") == session.get("user_id")


def _can_read(request: web.Request, session: dict) -> bool:
    tg_user = request.get("tg_user")
    if tg_user is None:  # DEV_MODE
        return True
    uid = tg_user.get("id")
    if uid == session.get("user_id"):
        return True
    return uid in (session.get("viewers") or [])


# ─── Bot Handlers ───────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    # Check if started with a deep link (session ID for listen mode)
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("listen_"):
        session_id = args[1].replace("listen_", "")
        session = store.get_session(session_id)
        if session:
            await send_listen_options(message, session)
            return

    await message.answer(
        "🎙️ <b>Audio Comments Bot</b>\n\n"
        "Send me a voice message or audio file, and I'll let you add "
        "voice comments at specific timestamps.\n\n"
        "<b>How it works:</b>\n"
        "1. Send or forward a voice/audio message here\n"
        "2. Tap <b>Comment on Audio</b> to open the player\n"
        "3. Listen, and tap Record whenever you want to add a comment\n"
        "4. When done, your comments get stitched into the audio\n"
        "5. Send the result to your friend!\n\n"
        "Your friend can then listen with <b>skip-to-comments</b> mode, "
        "or reply with their own comments.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🎙️ <b>Usage:</b>\n\n"
        "• Send any voice message or audio file\n"
        "• Use the Mini App to record comments\n"
        "• Forward the result to your friend\n"
        "• They can forward it back here to listen or reply\n\n"
        "📎 <b>Tip:</b> You can forward voice messages from any chat!",
        parse_mode=ParseMode.HTML,
    )


def _lookup_parent_session(message: Message, voice) -> dict | None:
    """
    Identify the parent session for an incoming audio, if any.

    Tries the ACID:<id> caption first; falls back to a file_unique_id index so
    captionless forwards (Telegram mobile lets users strip captions) still match.
    Returns the session dict or None.
    """
    # 1) Caption-embedded session ID — works across devices/clients.
    if message.caption and "ACID:" in message.caption:
        try:
            pid = message.caption.split("ACID:", 1)[1].strip().split()[0]
            sess = store.get_session(pid)
            if sess:
                return sess
        except Exception:
            pass

    # 2) file_unique_id is stable across forwards even without a caption.
    fuid = getattr(voice, "file_unique_id", None)
    if fuid:
        sess = store.get_session_by_file_unique_id(fuid)
        if sess:
            return sess
    return None


@router.message(F.voice | F.audio)
async def handle_audio_message(message: Message):
    """Handle incoming voice messages and audio files."""
    voice = message.voice or message.audio
    if not voice:
        return

    # Detect a returning stitched voice (forward-back) before doing any work.
    # Two paths: ACID:<id> caption, and file_unique_id index (caption-loss safe).
    parent_session = _lookup_parent_session(message, voice)
    if parent_session and parent_session.get("result_markers"):
        await send_listen_options(message, parent_session)
        return

    # Reject oversize input before downloading — pydub loads the decoded PCM
    # fully into memory, so a 1-hour podcast can OOM the process.
    duration_s = voice.duration or 0
    if duration_s > MAX_AUDIO_SECONDS:
        await message.answer(
            f"❌ Audio too long ({format_duration(duration_s * 1000)}). "
            f"Max supported is {MAX_AUDIO_SECONDS // 60} min."
        )
        return
    file_size = getattr(voice, "file_size", None) or 0
    if file_size > MAX_AUDIO_MB * 1024 * 1024:
        await message.answer(
            f"❌ Audio file too large ({file_size // (1024 * 1024)} MB). "
            f"Max supported is {MAX_AUDIO_MB} MB."
        )
        return

    await message.answer("⏳ Processing audio...")

    # Show a live "recording a voice message" indicator under the bot's name
    # while we download + convert (which can take 5-10s for longer audio).
    async with chat_action_loop(message.chat.id, "record_voice"):
        # Download the audio file
        file = await bot.get_file(voice.file_id)
        file_ext = "ogg" if message.voice else (file.file_path.split(".")[-1] if file.file_path else "ogg")
        filename = f"orig_{store.new_id()}.{file_ext}"
        filepath = store.audio_path(filename)
        await bot.download_file(file.file_path, filepath)

        # Get duration
        duration_ms = (voice.duration or 0) * 1000
        if duration_ms == 0:
            try:
                duration_ms = await asyncio.to_thread(audio_processor.get_duration_ms, filename)
            except Exception:
                duration_ms = 0

        # Convert to MP3 for web playback
        mp3_filename = filename.rsplit(".", 1)[0] + ".mp3"
        try:
            await asyncio.to_thread(audio_processor.convert_to_mp3, filename, mp3_filename)
        except Exception as e:
            logger.error(f"MP3 conversion failed: {e}")
            await message.answer("❌ Failed to process audio. Make sure ffmpeg is installed.")
            return

    # If we found a non-stitched parent (still recording), preserve the link.
    parent_session_id = parent_session["id"] if parent_session else None

    # Create session
    session = store.create_session(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        original_audio=filename,
        original_duration_ms=duration_ms,
        parent_session=parent_session_id,
        parent_markers=[],
    )

    # Store the mp3 reference
    await store.update_session(session["id"], original_audio_web=mp3_filename)

    # Send response with Mini App button
    webapp_url = f"{BASE_URL}/app?session={session['id']}&mode=comment"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎙️ Comment on Audio",
            web_app=WebAppInfo(url=webapp_url),
        )],
    ])

    duration_str = format_duration(duration_ms)
    await message.answer(
        f"🎧 Audio received ({duration_str})\n\n"
        f"Tap below to open the player and add your voice comments:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("listen:"))
async def handle_listen_callback(callback: CallbackQuery):
    session_id = callback.data.split(":", 1)[1]
    session = store.get_session(session_id)
    if not session:
        await callback.answer("Session not found", show_alert=True)
        return

    await _grant_viewer_access(session, callback.from_user.id)
    webapp_url = f"{BASE_URL}/app?session={session_id}&mode=listen"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎧 Open Player",
            web_app=WebAppInfo(url=webapp_url),
        )],
    ])
    await callback.message.answer(
        "🎧 Opening audio player...",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reply:"))
async def handle_reply_callback(callback: CallbackQuery):
    session_id = callback.data.split(":", 1)[1]
    session = store.get_session(session_id)
    if not session or not session.get("result_audio"):
        await callback.answer("Session not found", show_alert=True)
        return

    # Create a new session with the result as the original
    new_session = store.create_session(
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        original_audio=session["result_audio"],
        original_duration_ms=session.get("result_duration_ms", 0),
        parent_session=session_id,
        parent_markers=session.get("result_markers", []),
    )
    # Copy over the web version
    if session.get("result_audio_web"):
        await store.update_session(new_session["id"], original_audio_web=session["result_audio_web"])

    webapp_url = f"{BASE_URL}/app?session={new_session['id']}&mode=comment"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎙️ Record Comments",
            web_app=WebAppInfo(url=webapp_url),
        )],
    ])
    await callback.message.answer(
        "🎙️ Record your comments on this audio:",
        reply_markup=keyboard,
    )
    await callback.answer()


async def _grant_viewer_access(session: dict, user_id: int) -> None:
    """Add user_id to the session's viewers list (read-only access)."""
    if user_id == session.get("user_id"):
        return  # owner already has full access
    viewers = list(session.get("viewers") or [])
    if user_id in viewers:
        return
    viewers.append(user_id)
    await store.update_session(session["id"], viewers=viewers)


async def send_listen_options(message: Message, session: dict):
    """Send options to listen to a stitched audio with comments."""
    session_id = session["id"]
    # Grant the requester read access so the Mini App can fetch the session/audio.
    await _grant_viewer_access(session, message.from_user.id)

    webapp_listen_url = f"{BASE_URL}/app?session={session_id}&mode=listen"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎧 Listen (Skip to Comments)",
            web_app=WebAppInfo(url=webapp_listen_url),
        )],
        [InlineKeyboardButton(
            text="💬 Reply with Comments",
            callback_data=f"reply:{session_id}",
        )],
    ])

    n_comments = len(session.get("result_markers", []))
    duration_str = format_duration(session.get("result_duration_ms", 0))

    await message.answer(
        f"🎧 <b>Audio with {n_comments} comment(s)</b> ({duration_str})\n\n"
        f"Listen with smart skipping (jumps to each comment with context), "
        f"or reply with your own comments.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def send_result(session: dict):
    """Send the stitched audio result back to the user."""
    session_id = session["id"]
    chat_id = session["chat_id"]

    result_path = store.audio_path(session["result_audio"])
    markers = session.get("result_markers", [])
    n_comments = len(markers)
    duration_ms = session.get("result_duration_ms", 0)
    duration_str = format_duration(duration_ms)

    # Send the audio file. Long results go as send_audio (music file) so the
    # recipient gets a proper player with scrubbing; short ones stay as voice
    # bubbles, which feel native in chat. Threshold mirrors Telegram's own
    # heuristic for "this is more than a quick voice note".
    audio_file = FSInputFile(str(result_path))
    header = f"🎙️ Audio with {n_comments} comment(s) • {duration_str}"
    acid_line = f"📎 ACID:{session_id}"
    caption, overflow_text = _build_caption_with_chapters(
        header, markers, acid_line, TELEGRAM_CAPTION_LIMIT
    )

    # This is the call we most need to survive transient errors — the stitch
    # has already completed (CPU spent, output file written) and a lost
    # delivery means the user sees an error message for work that succeeded.
    if duration_ms > LONG_RESULT_THRESHOLD_MS:
        sent = await with_telegram_retry(
            lambda: bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                caption=caption,
                duration=duration_ms // 1000,
                title=f"Audio with {n_comments} comment(s)",
            ),
            what=f"send_audio session={session_id}",
        )
    else:
        sent = await with_telegram_retry(
            lambda: bot.send_voice(
                chat_id=chat_id,
                voice=audio_file,
                caption=caption,
                duration=duration_ms // 1000,
            ),
            what=f"send_voice session={session_id}",
        )

    # Index by file_unique_id so the bot still recognizes this audio if the
    # caption is stripped on forward (Telegram mobile allows that).
    media = (sent.voice if sent else None) or (sent.audio if sent else None)
    if media and media.file_unique_id:
        await store.set_file_unique_id(media.file_unique_id, session_id)

    # If the chapter list didn't fit in the caption, send it as a follow-up.
    # Telegram messages cap at 4096 chars; if even the overflow exceeds that
    # we just truncate — full search-by-text isn't promised by this feature.
    if overflow_text:
        msg = overflow_text if len(overflow_text) <= 4000 else overflow_text[:3997] + "..."
        try:
            await with_telegram_retry(
                lambda: bot.send_message(chat_id=chat_id, text=msg),
                what=f"chapter follow-up session={session_id}",
            )
        except Exception as e:
            logger.warning(f"session={session_id}: chapter follow-up send failed: {e}")

    # Send control buttons
    listen_url = f"{BASE_URL}/app?session={session_id}&mode=listen"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎧 Listen (Skip to Comments)",
            web_app=WebAppInfo(url=listen_url),
        )],
        [InlineKeyboardButton(
            text="💬 Reply with Comments",
            callback_data=f"reply:{session_id}",
        )],
    ])

    await with_telegram_retry(
        lambda: bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Done!</b> {n_comments} comment(s) stitched in.\n\n"
                "⬆️ Forward the audio above to your friend.\n"
                "They can forward it back to this bot to listen with skip-to-comments "
                "or add their own replies.\n\n"
                "Or use the buttons below yourself:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        ),
        what=f"done-message session={session_id}",
    )


@contextlib.asynccontextmanager
async def chat_action_loop(chat_id: int, action: str = "record_voice"):
    """Keep `action` flashing under the bot's name while a slow handler runs.

    Telegram clears chat actions automatically ~5s after the last call, so a
    long download/convert would otherwise look frozen. We re-emit every 4s.
    Network errors are swallowed — the indicator is best-effort cosmetic.
    """
    async def loop():
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception:
                pass
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def format_duration(ms: int) -> str:
    seconds = ms // 1000
    minutes = seconds // 60
    secs = seconds % 60
    if minutes > 0:
        return f"{minutes}:{secs:02d}"
    return f"0:{secs:02d}"


# Cap on send-retry attempts. 4 is plenty for both rate-limits (each retry
# waits the server's stated retry_after) and transient errors (exponential
# backoff). Beyond this we surface the failure to the caller.
_SEND_RETRY_ATTEMPTS = 4


async def with_telegram_retry(coro_factory, *, what: str, attempts: int = _SEND_RETRY_ATTEMPTS):
    """Retry a Telegram Bot API call on rate-limit and transient errors.

    The biggest cost we want to avoid is finishing a stitch (10+ seconds of
    CPU work) and then losing the result because Telegram returned 429. Used
    around `send_result`'s critical sends.

    `coro_factory` must be a zero-arg callable returning a fresh coroutine —
    coroutines can't be awaited twice, so the caller wraps the call in a
    lambda. `what` is a short label for logs.
    """
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            # Telegram tells us exactly how long to wait. Add a small buffer
            # so a clock-skew off-by-one doesn't cause an immediate re-429.
            wait = float(e.retry_after) + 0.5
            logger.warning(
                f"{what}: 429 rate-limited, retrying in {wait:.1f}s "
                f"(attempt {attempt}/{attempts})"
            )
            last_exc = e
            await asyncio.sleep(wait)
        except (TelegramNetworkError, TelegramServerError) as e:
            last_exc = e
            if attempt == attempts:
                break
            logger.warning(
                f"{what}: {type(e).__name__}: {e}, retrying in {delay:.1f}s "
                f"(attempt {attempt}/{attempts})"
            )
            await asyncio.sleep(delay)
            delay *= 2
    # Re-raise the most recent failure rather than wrapping it — callers can
    # match on the original exception type if they need to.
    assert last_exc is not None
    raise last_exc


# Per-chapter line length cap. Whisper output for a 30s clip can run hundreds
# of chars; long lines drown out the rest of the chapter list.
_CHAPTER_LINE_MAX = 90


def _format_chapter_line(idx: int, marker: dict) -> str:
    """`1. 0:12 — "test one two three"` (or no quoted text if no transcript)."""
    ts = format_duration(int(marker.get("start_ms") or 0))
    text = (marker.get("transcript") or "").strip()
    if not text:
        return f"{idx}. {ts}"
    if len(text) > _CHAPTER_LINE_MAX:
        text = text[: _CHAPTER_LINE_MAX - 1].rstrip() + "…"
    return f"{idx}. {ts} — “{text}”"


def _build_caption_with_chapters(
    header: str,
    markers: list[dict],
    acid_line: str,
    caption_limit: int,
) -> tuple[str, str | None]:
    """Pack `header` + numbered chapter lines + `acid_line` into one caption.

    Returns `(caption, overflow_text)`. If the full list fits, `overflow_text`
    is None. Otherwise the caption keeps header + ACID (forward-back detection
    must survive) and the chapter list ships as a separate text message.
    """
    if not markers:
        return f"{header}\n\n{acid_line}", None

    chapter_lines = [_format_chapter_line(i + 1, m) for i, m in enumerate(markers)]
    chapter_block = "\n".join(chapter_lines)
    full = f"{header}\n\n{chapter_block}\n\n{acid_line}"
    if len(full) <= caption_limit:
        return full, None

    short_caption = f"{header}\n\n{acid_line}"
    overflow = f"{header}\n\n{chapter_block}"
    return short_caption, overflow


# ─── Web Server (API + Mini App) ───────────────────────────────────────────────

routes = web.RouteTableDef()


@routes.get("/healthz")
async def healthz(request: web.Request):
    """Liveness probe for load balancers / container orchestrators.

    Returns 200 unconditionally — this is a *liveness* check (process is up
    and the event loop is responsive), not readiness. We deliberately do not
    call the Telegram API here so a transient bot-side outage doesn't cause
    pods to flap.
    """
    return web.json_response({"status": "ok"})


@routes.get("/app")
async def serve_webapp(request: web.Request):
    """Serve the Mini App HTML."""
    return web.FileResponse(
        Path(__file__).parent / "webapp" / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@routes.get("/api/session/{session_id}")
async def api_get_session(request: web.Request):
    """Get session info (used by Mini App)."""
    session_id = request.match_info["session_id"]
    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _can_read(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    # Return safe subset of session data
    return web.json_response({
        "id": session["id"],
        "original_duration_ms": session.get("original_duration_ms", 0),
        "recordings": [
            {
                "timestamp_ms": r["timestamp_ms"],
                "duration_ms": r["duration_ms"],
                "index": i,
                # null until the background transcriber has filled it in; the
                # Mini App polls for this so users see "Transcribing…" → text.
                "transcript": r.get("transcript"),
            }
            for i, r in enumerate(session.get("recordings", []))
        ],
        "result_markers": session.get("result_markers"),
        "result_duration_ms": session.get("result_duration_ms"),
        "parent_markers": session.get("parent_markers", []),
        "status": session.get("status"),
        "context_seconds": CONTEXT_SECONDS,
        "has_result": session.get("result_audio") is not None,
        "transcription_enabled": transcriber.is_available(),
    })


@routes.get("/api/audio/{session_id}/{which}")
async def api_serve_audio(request: web.Request):
    """
    Serve audio files for web playback.
    which = 'original' or 'result'
    """
    session_id = request.match_info["session_id"]
    which = request.match_info["which"]
    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _can_read(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    if which == "original":
        filename = session.get("original_audio_web") or session.get("original_audio")
    elif which == "result":
        filename = session.get("result_audio_web") or session.get("result_audio")
    else:
        return web.json_response({"error": "Invalid"}, status=400)

    if not filename:
        return web.json_response({"error": "Audio not available"}, status=404)

    filepath = store.audio_path(filename)
    if not filepath.exists():
        return web.json_response({"error": "File not found"}, status=404)

    content_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/ogg"

    return web.FileResponse(
        filepath,
        headers={
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


@routes.post("/api/session/{session_id}/upload")
async def api_upload_recording(request: web.Request):
    """Upload a recorded audio chunk from the Mini App."""
    session_id = request.match_info["session_id"]
    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _is_owner(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    reader = await request.multipart()

    timestamp_ms = None
    audio_filename = None
    max_bytes = MAX_RECORDING_MB * 1024 * 1024
    oversize = False

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "timestamp_ms":
            timestamp_ms = int(await part.text())
        elif part.name == "audio":
            # Determine extension from content type
            content_type = part.headers.get("Content-Type", "audio/webm")
            ext = "webm"
            if "ogg" in content_type:
                ext = "ogg"
            elif "mp4" in content_type or "m4a" in content_type:
                ext = "m4a"
            elif "wav" in content_type:
                ext = "wav"

            audio_filename = f"rec_{session_id}_{store.new_id()}.{ext}"
            filepath = store.audio_path(audio_filename)
            bytes_written = 0
            with open(filepath, "wb") as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        oversize = True
                        break
                    f.write(chunk)
            if oversize:
                try:
                    filepath.unlink()
                except OSError:
                    pass
                audio_filename = None
                break

    if oversize:
        return web.json_response(
            {"error": f"Recording too large (max {MAX_RECORDING_MB} MB)"},
            status=413,
        )

    if timestamp_ms is None or not audio_filename:
        return web.json_response({"error": "Missing data"}, status=400)

    # Get recording duration
    try:
        duration_ms = await asyncio.to_thread(audio_processor.get_duration_ms, audio_filename)
    except Exception as e:
        logger.error(f"session={session_id}: failed to get recording duration: {e}")
        return web.json_response({"error": "Invalid audio"}, status=400)

    session = await store.add_recording(session_id, timestamp_ms, audio_filename, duration_ms)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)

    # Kick off Whisper transcription in the background. The task writes back to
    # the session JSON when done; the Mini App polls and re-renders. Fire-and-
    # forget — failures are logged inside transcribe_recording.
    if transcriber.ENABLED:
        asyncio.create_task(
            transcriber.transcribe_recording(session_id, audio_filename)
        )

    logger.info(
        f"session={session_id}: uploaded recording #{len(session['recordings']) - 1} "
        f"at {timestamp_ms}ms ({duration_ms}ms long)"
    )
    return web.json_response({
        "ok": True,
        "recording": {
            "timestamp_ms": timestamp_ms,
            "duration_ms": duration_ms,
            "index": len(session["recordings"]) - 1,
        },
    })


_RECORDING_CONTENT_TYPES = {
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}


@routes.get("/api/session/{session_id}/recording/{index}")
async def api_serve_recording(request: web.Request):
    """Serve a single pre-stitch recording so the Mini App can preview-play it.

    Owner-only: recordings only exist before stitching and are private to the
    creator. Index is the position in `session.recordings`.
    """
    session_id = request.match_info["session_id"]
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return web.json_response({"error": "Invalid"}, status=400)

    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _is_owner(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    recordings = session.get("recordings") or []
    if index < 0 or index >= len(recordings):
        return web.json_response({"error": "Not found"}, status=404)

    filename = recordings[index].get("filename")
    if not filename:
        return web.json_response({"error": "Not found"}, status=404)
    filepath = store.audio_path(filename)
    if not filepath.exists():
        return web.json_response({"error": "File not found"}, status=404)

    ext = filename.rsplit(".", 1)[-1].lower()
    content_type = _RECORDING_CONTENT_TYPES.get(ext, "application/octet-stream")

    return web.FileResponse(
        filepath,
        headers={
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


@routes.delete("/api/session/{session_id}/recording/{index}")
async def api_delete_recording(request: web.Request):
    """Delete a recording from a session."""
    session_id = request.match_info["session_id"]
    index = int(request.match_info["index"])

    # Ownership check before mutating.
    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _is_owner(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    session = await store.remove_recording(session_id, index)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response({"ok": True})


# ─── Stitch job tracking ───────────────────────────────────────────────────────
# Long stitches (10+ seconds for multi-minute audio) would block the POST and
# eventually time out in the Mini App. Instead the API returns immediately and
# the client polls /stitch_status. Job state is in-memory: on server restart a
# poller will see "idle" + result_audio on the session, which is good enough.

_stitch_jobs: dict[str, dict] = {}
_stitch_jobs_lock = asyncio.Lock()

# Bound concurrent stitches across the whole process. pydub holds the entire
# decoded PCM of both the original and the growing result in RAM, so two
# 40-min jobs at once can push peak RSS into the gigabytes and OOM the host.
# Lazy-initialized so the semaphore binds to the running event loop.
STITCH_MAX_PARALLEL = max(1, int(os.getenv("STITCH_MAX_PARALLEL", "1")))
_stitch_semaphore: asyncio.Semaphore | None = None


def _get_stitch_semaphore() -> asyncio.Semaphore:
    global _stitch_semaphore
    if _stitch_semaphore is None:
        _stitch_semaphore = asyncio.Semaphore(STITCH_MAX_PARALLEL)
    return _stitch_semaphore

# In-flight task tracking: we keep strong references so tasks don't get
# garbage-collected mid-run (asyncio docs explicitly warn about this) and so
# the shutdown path can wait for them to finish.
_stitch_tasks: set[asyncio.Task] = set()
_eviction_tasks: set[asyncio.Task] = set()

# How long a terminal (done/error) stitch job stays in _stitch_jobs before
# being evicted. The Mini App polls every 500ms during the stitch, so a few
# minutes is far more than enough; after that the persisted session.status
# fallback in api_stitch_status takes over.
STITCH_JOB_TTL_SECONDS = int(os.getenv("STITCH_JOB_TTL_SECONDS", "300"))

# How long graceful shutdown waits for in-flight stitch jobs to finish before
# giving up and letting the process exit. Keep this comfortably above the
# expected worst-case stitch time (a few seconds per minute of audio).
SHUTDOWN_DRAIN_SECONDS = int(os.getenv("SHUTDOWN_DRAIN_SECONDS", "120"))


async def _evict_job_later(session_id: str):
    """Sleep then remove a terminal job entry from _stitch_jobs.

    Each call is one-shot — _run_stitch_job schedules this when it sets a
    done/error state. The sleep is cancellable on shutdown.
    """
    try:
        await asyncio.sleep(STITCH_JOB_TTL_SECONDS)
    except asyncio.CancelledError:
        return
    _stitch_jobs.pop(session_id, None)


def _schedule_job_eviction(session_id: str) -> None:
    task = asyncio.create_task(_evict_job_later(session_id))
    _eviction_tasks.add(task)
    task.add_done_callback(_eviction_tasks.discard)


async def _drain_stitch_tasks(timeout: float) -> None:
    """Wait for in-flight stitch tasks to complete, up to `timeout` seconds.

    Called on shutdown so a SIGTERM mid-stitch doesn't corrupt the result
    audio (export is partway through writing the output file). Eviction
    tasks are cancelled rather than awaited — they're just sleep timers.
    """
    in_flight = [t for t in _stitch_tasks if not t.done()]
    if in_flight:
        logger.info(
            f"shutdown: waiting up to {timeout:.0f}s for "
            f"{len(in_flight)} in-flight stitch job(s) to finish"
        )
        done, pending = await asyncio.wait(in_flight, timeout=timeout)
        if pending:
            logger.warning(
                f"shutdown: {len(pending)} stitch job(s) still running after "
                f"{timeout:.0f}s — they will be cancelled"
            )
            for t in pending:
                t.cancel()
    for t in list(_eviction_tasks):
        t.cancel()


async def _wait_for_transcripts(session_id: str, timeout_s: int) -> dict:
    """Re-read the session every 500ms until every recording has a transcript
    or `timeout_s` elapses. Returns the most recent session dict.

    Background tasks write transcripts into the session JSON as they finish; we
    just poll for completion rather than tracking per-task futures (those would
    need a registry across upload/stitch handlers).
    """
    deadline = asyncio.get_event_loop().time() + max(0, timeout_s)
    while True:
        session = store.get_session(session_id)
        if not session:
            return session  # caller handles None
        recs = session.get("recordings") or []
        missing = sum(1 for r in recs if not r.get("transcript"))
        if missing == 0:
            return session
        if asyncio.get_event_loop().time() >= deadline:
            logger.info(
                f"session={session_id}: stitching with {missing}/{len(recs)} "
                f"transcript(s) still pending (timeout)"
            )
            return session
        await asyncio.sleep(0.5)


async def _run_stitch_job(session_id: str, rid: str):
    """Background worker — does the stitch + send_result, updates _stitch_jobs."""
    token = request_id_var.set(rid)
    try:
        def progress_cb(p: float):
            # Called from the worker thread; dict write is GIL-atomic.
            job = _stitch_jobs.get(session_id)
            if job is not None:
                job["progress"] = float(p)

        try:
            session = store.get_session(session_id)
            if not session:
                _stitch_jobs[session_id] = {"status": "error", "error": "Session not found"}
                return

            # Give any in-flight background transcriptions a brief chance to
            # land so the chapter caption + listen-mode tooltips actually have
            # text. Bounded by TRANSCRIBE_WAIT_SECONDS; we proceed with whatever
            # transcripts are present at the deadline.
            if transcriber.is_available():
                session = await _wait_for_transcripts(session_id, TRANSCRIBE_WAIT_SECONDS)

            logger.info(f"session={session_id}: stitch starting ({len(session['recordings'])} rec)")
            sem = _get_stitch_semaphore()
            async with sem:
                result = await asyncio.to_thread(audio_processor.stitch, session, progress_cb)

            session = await store.update_session(session_id, **result)
            if not session:
                _stitch_jobs[session_id] = {"status": "error", "error": "Session disappeared"}
                return

            await send_result(session)
            await store.update_session(session_id, status="sent")

            _stitch_jobs[session_id] = {
                "status": "done",
                "progress": 1.0,
                "markers": session["result_markers"],
                "duration_ms": session.get("result_duration_ms", 0),
            }
            logger.info(f"session={session_id}: stitch done")
        except Exception as e:
            logger.error(f"session={session_id}: stitch failed: {e}", exc_info=True)
            _stitch_jobs[session_id] = {"status": "error", "error": str(e)}
    finally:
        # Terminal state reached — schedule eviction so the dict doesn't grow
        # unbounded. Safe even when the entry is already absent (Session
        # disappeared branch) — pop is idempotent.
        _schedule_job_eviction(session_id)
        request_id_var.reset(token)


@routes.post("/api/session/{session_id}/stitch")
async def api_stitch(request: web.Request):
    """Kick off audio stitching. Returns immediately; client polls /stitch_status."""
    session_id = request.match_info["session_id"]
    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _is_owner(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    if not session["recordings"]:
        return web.json_response({"error": "No recordings to stitch"}, status=400)

    async with _stitch_jobs_lock:
        existing = _stitch_jobs.get(session_id)
        if existing and existing.get("status") == "running":
            # Already in flight — return its current state without starting a second.
            return web.json_response({
                "ok": True,
                "status": "running",
                "progress": existing.get("progress", 0.0),
            })
        _stitch_jobs[session_id] = {"status": "running", "progress": 0.0}

    # Strong-reference the task and remove on completion so it isn't GC'd
    # mid-run, and so graceful shutdown can wait for it.
    task = asyncio.create_task(_run_stitch_job(session_id, request.get("request_id", "-")))
    _stitch_tasks.add(task)
    task.add_done_callback(_stitch_tasks.discard)
    return web.json_response({"ok": True, "status": "running", "progress": 0.0})


@routes.get("/api/session/{session_id}/stitch_status")
async def api_stitch_status(request: web.Request):
    """Return the current stitch job state for the Mini App's poller."""
    session_id = request.match_info["session_id"]
    session = store.get_session(session_id)
    if not session:
        return web.json_response({"error": "Not found"}, status=404)
    if not _can_read(request, session):
        return web.json_response({"error": "Forbidden"}, status=403)

    job = _stitch_jobs.get(session_id)
    if job:
        return web.json_response(job)

    # No in-memory job (server may have restarted, or this is a fresh session).
    # Fall back to the persisted session status.
    if session.get("status") in ("stitched", "sent") and session.get("result_audio"):
        return web.json_response({
            "status": "done",
            "progress": 1.0,
            "markers": session.get("result_markers"),
            "duration_ms": session.get("result_duration_ms", 0),
        })
    return web.json_response({"status": "idle"})


# Request ID middleware — mints a token per API call and binds it to the
# contextvar so every log line emitted while handling that request carries it.
# Outermost in the stack so even 401/403 responses (from auth_middleware) log
# under a stable ID.
@web.middleware
async def request_id_middleware(request, handler):
    rid = _new_request_id()
    request["request_id"] = rid
    token = request_id_var.set(rid)
    try:
        return await handler(request)
    finally:
        request_id_var.reset(token)


# CORS middleware — Mini App is served same-origin as the API, so we mirror the
# configured origin. Any other Origin gets the same allow list and is rejected
# by the browser. The auth middleware does the actual gate-keeping.
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = e
    response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    response.headers["Vary"] = "Origin"
    return response


# Auth middleware — verifies Telegram WebApp initData on every API call.
# Skipped for the Mini App HTML (`/app`) which Telegram loads directly, and for
# CORS preflight (OPTIONS — preflights never carry the custom header).
@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if request.method == "OPTIONS" or path == "/app" or not path.startswith("/api/"):
        return await handler(request)

    if DEV_MODE:
        # No tg_user stashed; _is_owner / _can_read short-circuit to True.
        return await handler(request)

    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        # Fallback for <audio> elements, which can't send custom headers.
        init_data = request.query.get("tgInitData", "")

    user = verify_webapp_data(init_data)
    if not user:
        return web.json_response({"error": "Unauthorized"}, status=401)

    request["tg_user"] = user
    return await handler(request)


# ─── Main ───────────────────────────────────────────────────────────────────────

async def main():
    # Order matters: request_id is outermost so logs from CORS / auth carry it,
    # then CORS so its headers attach even to 401/403 produced by auth.
    app = web.Application(
        middlewares=[request_id_middleware, cors_middleware, auth_middleware]
    )
    app.add_routes(routes)

    use_webhook = MODE == "webhook"
    if use_webhook:
        if not WEBHOOK_SECRET_PATH or not WEBHOOK_SECRET_TOKEN:
            raise RuntimeError(
                "MODE=webhook requires WEBHOOK_SECRET_PATH and WEBHOOK_SECRET_TOKEN"
            )
        # SimpleRequestHandler validates the X-Telegram-Bot-Api-Secret-Token
        # header automatically and forwards the update to the dispatcher.
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=WEBHOOK_SECRET_TOKEN,
        ).register(app, path=WEBHOOK_SECRET_PATH)
        # Wires aiogram's startup/shutdown signals onto the aiohttp app.
        setup_application(app, dp, bot=bot)

    # Start web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")
    logger.info(f"Mini App URL: {BASE_URL}/app")

    # Register the slash-command menu so clients show /start and /help in the
    # "/" picker. Idempotent — safe to call every startup.
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="help", description="How to use"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")

    # Background session cleanup (TTL-based).
    cleanup_task = asyncio.create_task(cleanup.run_cleanup_loop())

    # Signal-driven shutdown for webhook mode. (Polling mode's `start_polling`
    # installs its own SIGINT/SIGTERM handlers and returns when triggered, so
    # we only need to bridge the gap for the webhook `Event().wait()` below.)
    stop_event = asyncio.Event()
    if use_webhook:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main-thread / restricted envs: signal handlers
                # aren't available, fall back to the process being killed.
                pass

    try:
        if use_webhook:
            await bot.set_webhook(
                url=BASE_URL.rstrip("/") + WEBHOOK_SECRET_PATH,
                secret_token=WEBHOOK_SECRET_TOKEN,
                allowed_updates=dp.resolve_used_update_types(),
            )
            logger.info(f"Webhook registered at {WEBHOOK_SECRET_PATH}; waiting for updates...")
            await stop_event.wait()
            logger.info("Shutdown signal received")
        else:
            logger.info("Starting bot polling...")
            await dp.start_polling(bot)
    finally:
        # Drain in-flight stitches first — losing one mid-export corrupts the
        # output file and the user sees "error" for work that was almost done.
        try:
            await _drain_stitch_tasks(SHUTDOWN_DRAIN_SECONDS)
        except Exception as e:
            logger.warning(f"shutdown: drain failed: {e}")

        cleanup_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):
            pass

        # Close the bot session so pending HTTP connections don't leak.
        with contextlib.suppress(Exception):
            await bot.session.close()

        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
