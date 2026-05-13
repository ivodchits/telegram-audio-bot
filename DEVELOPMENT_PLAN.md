# Telegram Audio Comments Bot — Development Plan

This document is a step-by-step plan to take the current implementation in `telegram-audio-comments/` from "demo that works for one person at a time" to "production-ready bot". Each step lists:

- **What** — the change in concrete terms
- **Where** — files / line ranges to touch
- **Why** — the problem it solves
- **How** — implementation sketch
- **Done when** — acceptance criteria

Work top to bottom. Phases 1–3 are blocking real-user use; phases 4–5 are operational; phase 6 is polish; phase 7 is the stretch transcription feature.

---

## Phase 1 — Correctness Bugs

These items are cases where the implementation does not do what the README promises, or where shared state is corrupted under normal use.

### 1.1 Fix the "forward back → Listen" flow

- **What:** When a user (or their friend) forwards a previously stitched voice message back to the bot, offer the Listen / Reply choice instead of always creating a fresh commenting session.
- **Where:** `main.py:123-196` (`handle_audio_message`), `main.py:256-283` (`send_listen_options`).
- **Why:** The README's "Listen Flow" §1–4 promises this UX. Today it is unreachable: the only caller of `send_listen_options` is the `/start listen_<id>` deep-link branch in `cmd_start` (`main.py:86-92`), and no code path anywhere generates that deep link.
- **How:**
  1. In `handle_audio_message`, after extracting `parent_session_id` from the `ACID:` caption (`main.py:159-166`), look up the parent session.
  2. If the parent has `result_markers` (i.e. it has been stitched) **and** the forwarder is not the original commenter, branch:
     - Call `send_listen_options(message, parent_session)` and return — do not create a new session yet.
     - The "Reply with Comments" callback (`main.py:221`) already creates the new session, so deferring is safe.
  3. If the parent has `result_markers` and the forwarder *is* the original commenter, still show Listen + Reply (they may want to re-listen to their own work).
  4. If `ACID:` is missing or the parent is gone, fall through to the existing "create comment session" path.
- **Done when:** Forwarding a stitched voice back to the bot from a second Telegram account shows both "Listen (Skip to Comments)" and "Reply with Comments" buttons, and the listen button opens the Mini App in listen mode against the correct session.

### 1.2 Make reply-chain detection robust to caption loss

- **What:** Recognize a returning stitched voice even when the `ACID:<id>` caption was stripped.
- **Where:** `store.py` (new index file), `main.py:123-196`.
- **Why:** Telegram mobile clients let users delete captions on forward, and some chat types strip captions silently. The current `ACID:` parse (`main.py:159`) is the single source of truth for "this is a returning audio".
- **How:**
  1. Add a JSON index `data/sessions/_by_file_unique_id.json` mapping `file_unique_id -> session_id`. (`file_unique_id` is stable across forwards, unlike `file_id`.)
  2. When the bot sends the stitched voice in `send_result` (`main.py:286-333`), capture `message.voice.file_unique_id` from the returned `Message` and write the mapping.
  3. In `handle_audio_message`, before falling through to "new session", look up `voice.file_unique_id` in this index. If found, treat it the same as a successful `ACID:` parse.
  4. Keep the `ACID:` caption as a secondary mechanism — it is still useful for sessions that crossed devices/clients.
- **Done when:** A stitched voice forwarded between two users without the caption still triggers the Listen flow on the receiving side.

### 1.3 Stop blocking the event loop on audio work

- **What:** Wrap every pydub / ffmpeg call in `asyncio.to_thread(...)`.
- **Where:**
  - `main.py:143` — `audio_processor.get_duration_ms`
  - `main.py:150` — `audio_processor.convert_to_mp3`
  - `main.py:468` — `audio_processor.get_duration_ms`
  - `main.py:509` — `audio_processor.stitch`
- **Why:** pydub is synchronous and CPU-bound. A single 5-minute stitch can freeze the bot for tens of seconds — all other handlers (polling, callbacks, API requests) wait. With two concurrent users this becomes obvious.
- **How:** Each call becomes `await asyncio.to_thread(audio_processor.X, ...)`. No signature changes needed inside `audio_processor.py`.
- **Done when:** While one user's stitch is running, `/start` from a second account replies within ~1 second.

### 1.4 Add per-session locking in `store.py`

- **What:** Serialize read-modify-write on a single session JSON.
- **Where:** `store.py:68` (`update_session`), `store.py:77` (`add_recording`), `store.py:92` (`remove_recording`).
- **Why:** Two near-simultaneous uploads, or an upload racing with a stitch finalization, can lose writes. Symptom: a recording is uploaded successfully but vanishes from the session after stitch.
- **How:**
  1. Maintain a module-level `dict[session_id, asyncio.Lock]`.
  2. Convert `add_recording`, `remove_recording`, `update_session` to `async` and `async with _lock_for(session_id):` around the read+write.
  3. Update all call sites in `main.py` and `audio_processor.py` to `await` these.
  4. Alternative if you want to avoid touching call sites: use `filelock.FileLock` per session file (sync, works inside `to_thread`).
- **Done when:** A scripted test that uploads 5 recordings concurrently to one session ends with all 5 present in the JSON.

### 1.5 Make `DATA_DIR` location stable

- **What:** Resolve `data/` relative to the source file, not the current working directory.
- **Where:** `store.py:12-14`.
- **Why:** `Path("data")` resolves against `os.getcwd()`. Running `python telegram-audio-comments/main.py` from the repo root puts data in `./data/`; running from inside the subfolder puts it in `telegram-audio-comments/data/`. Two locations silently diverge.
- **How:**
  ```python
  _ROOT = Path(__file__).resolve().parent
  DATA_DIR = Path(os.getenv("DATA_DIR", _ROOT / "data"))
  ```
  Also add `DATA_DIR` to `.env.example`.
- **Done when:** `python telegram-audio-comments/main.py` and `cd telegram-audio-comments && python main.py` both read and write the same `data/` directory.

### 1.6 Remove dead import

- **What:** Drop `urlencode` from `main.py:15`.
- **Why:** Unused; signals stale code.
- **Done when:** `python -c "import main"` from `telegram-audio-comments/` still imports cleanly (or `ruff check` is clean if added later).

---

## Phase 2 — Security

### 2.1 Wire up Telegram WebApp `initData` verification on every API route

- **What:** Require a valid `initData` on `/api/session/*`, `/api/audio/*`, `/api/session/*/upload`, `/api/session/*/recording/*`, `/api/session/*/stitch`. Reject mismatched user IDs.
- **Where:** `main.py:48-78` (verifier already exists), `main.py:347-540` (all routes).
- **Why:** Session IDs are 12-hex-char (48 bits, `store.py:26`). With unauthenticated endpoints, anyone who guesses or scrapes an ID can read the audio, upload garbage recordings, or trigger stitching. The README explicitly flags this as TODO.
- **How:**
  1. Mini App must send `initData` with every request. Two options:
     - Header `X-Telegram-Init-Data: <raw initData string>` (cleanest).
     - Or include in form data for uploads, query string for GETs (avoid query string — it logs).
  2. Add an aiohttp middleware `auth_middleware` that:
     - Skips `/app` (the HTML itself — Telegram loads it directly).
     - Reads `X-Telegram-Init-Data`.
     - Calls `verify_webapp_data`.
     - On failure: 401.
     - On success: stash `user` on `request["tg_user"]`.
  3. In each route, compare `request["tg_user"]["id"]` against `session["user_id"]`. 403 if mismatch.
  4. In the Mini App (`webapp/index.html:519`), read `tg.initData` and pass it as a header in every `fetch`.
  5. Have a `DEV_MODE` env flag that skips verification, so local testing without a real bot still works.
- **Done when:** A `curl` to `/api/session/<any-id>` without an `X-Telegram-Init-Data` header returns 401; with a valid header for a different user, returns 403; with a valid header for the session's owner, returns 200.

### 2.2 Tighten CORS

- **What:** Replace `Access-Control-Allow-Origin: *` with the configured `BASE_URL` origin.
- **Where:** `main.py:527-539` (`cors_middleware`), `main.py:417` (audio response).
- **Why:** With auth in place (2.1), wildcard CORS is no longer needed. Keeps the API uncallable from random third-party pages.
- **How:** Read `BASE_URL`, parse origin, return that exact string in `Access-Control-Allow-Origin`. Same Mini App origin = same as API origin = no preflight issues.
- **Done when:** Requests from `BASE_URL` succeed; requests from a different `Origin` are rejected by the browser.

### 2.3 Lengthen session IDs

- **What:** Bump `new_id()` from 12 hex chars to 22+ (≥88 bits).
- **Where:** `store.py:25-26`.
- **Why:** Even with auth, defense in depth — long IDs make scraping infeasible if auth is ever bypassed or relaxed.
- **How:** `return secrets.token_urlsafe(16)`.
- **Done when:** Newly created sessions have IDs ≥22 characters. (Existing sessions continue to work — the change is forward-only.)

---

## Phase 3 — Resource Limits & Safety

### 3.1 Cap input audio size and duration

- **What:** Reject voice/audio over a configurable limit before download.
- **Where:** `main.py:123-196` (top of `handle_audio_message`).
- **Why:** A single 1-hour podcast forwarded to the bot triggers a full pydub decode (`audio_processor.py:30`), which loads the decoded PCM into memory. Easy OOM. Also wastes Telegram bandwidth.
- **How:**
  1. Add env vars `MAX_AUDIO_SECONDS` (default 600) and `MAX_AUDIO_MB` (default 25).
  2. Use `voice.duration` and `voice.file_size` from the incoming message *before* `bot.download_file`.
  3. On reject, reply with a friendly explanation; do not create a session.
- **Done when:** Sending a 30-minute voice message replies "audio too long" and creates no files in `data/`.

### 3.2 Cap comment recording size

- **What:** Reject uploads over a configurable size in the upload route.
- **Where:** `main.py:423-482` (`api_upload_recording`).
- **Why:** The Mini App streams uploads multipart; without a limit, an adversarial or buggy client can write unbounded bytes to disk.
- **How:**
  1. Track bytes written inside the `while True: chunk = await part.read_chunk()` loop. Abort and 413 if over `MAX_RECORDING_MB` (default 5).
  2. Delete the partial file on abort.
- **Done when:** A scripted upload of a 100 MB blob to `/api/session/.../upload` returns 413 and no file remains on disk.

### 3.3 Background cleanup of old sessions

- **What:** Periodically delete sessions and their audio files older than N days.
- **Where:** New file `cleanup.py`, started as an `asyncio.Task` from `main.py:544` (`main`).
- **Why:** Today, every session and audio file lives forever. Disk fills.
- **How:**
  1. `SESSION_TTL_DAYS` env var (default 7).
  2. Async task that wakes every hour, walks `data/sessions/`, parses `created_at`, deletes session JSON and all referenced audio files (original, original_web, result, result_web, and every recording filename).
  3. Also delete the `_by_file_unique_id.json` index entries for removed sessions.
  4. Log how many sessions were swept.
- **Done when:** A session backdated by `created_at` of 8 days ago is removed on the next sweep, along with its audio.

### 3.4 Use `send_audio` instead of `send_voice` for long results

- **What:** Pick the send method based on duration.
- **Where:** `main.py:286-333` (`send_result`).
- **Why:** Telegram's `send_voice` is optimized for short messages; very long stitched audio (~10+ min) is better delivered as a music file. Voice messages also can't be seeked precisely in some clients.
- **How:** If `duration_ms > 10 * 60 * 1000`, call `bot.send_audio(...)` with `title="Audio with N comments"`. Otherwise `send_voice` as today. Keep the caption + buttons identical.
- **Done when:** A stitched 15-minute audio arrives as a playable audio file (not a voice bubble) with all controls intact.

---

## Phase 4 — Production Operability

### 4.1 Webhook mode (in addition to polling)

- **What:** Optional webhook delivery served on the existing aiohttp app.
- **Where:** `main.py:544-566` (`main`).
- **Why:** Polling adds latency and rules out horizontal scaling. README flags this as TODO.
- **How:**
  1. Add env vars `MODE=polling|webhook`, `WEBHOOK_SECRET_PATH=/tg/<random>`, `WEBHOOK_SECRET_TOKEN=<random>`.
  2. If `MODE=webhook`:
     - On startup, call `bot.set_webhook(url=BASE_URL + WEBHOOK_SECRET_PATH, secret_token=WEBHOOK_SECRET_TOKEN, allowed_updates=[...])`.
     - Register an aiohttp route at `WEBHOOK_SECRET_PATH` that pipes incoming updates to `dp.feed_webhook_update`.
     - Validate `X-Telegram-Bot-Api-Secret-Token` header against `WEBHOOK_SECRET_TOKEN`.
  3. Else: existing polling.
- **Done when:** With `MODE=webhook`, sending a voice to the bot triggers `handle_audio_message` and the bot never makes a `getUpdates` call.

### 4.2 Streamed / chunked progress for stitching

- **What:** Replace the single blocking `POST /api/session/<id>/stitch` with kick-off + status polling.
- **Where:** `main.py:496-523`, `webapp/index.html:1046-1076` (`finish`).
- **Why:** Stitching a 10-minute audio with many comments can take 10+ seconds. The Mini App currently shows a spinner with no progress and an indefinite timeout.
- **How:**
  1. `POST /api/session/<id>/stitch` starts an `asyncio.Task` and returns `{"job_id": ..., "status": "running"}` immediately.
  2. Store job state on the session: `{"stitch_status": "running"|"done"|"error", "stitch_progress": 0..1, "stitch_error": "..."}`.
  3. Update `stitch_progress` from inside `audio_processor.stitch` after each recording is appended (pass a callback).
  4. New `GET /api/session/<id>/stitch_status` returns the state.
  5. Mini App polls every 500ms and updates the status overlay text ("Stitching… 60%").
- **Done when:** During a 10-second stitch, the Mini App shows a moving percentage and never times out.

### 4.3 Bot commands menu

- **What:** Register the slash-command menu so Telegram's UI shows `/start` and `/help`.
- **Where:** `main.py:544-566` (`main`).
- **Why:** Discoverability — without `setMyCommands`, the "/" menu in chat is empty.
- **How:** On startup, `await bot.set_my_commands([BotCommand(command="start", description="Start the bot"), BotCommand(command="help", description="How to use")])`.
- **Done when:** Typing `/` in chat with the bot shows the two commands with descriptions.

### 4.4 Structured logging and request IDs

- **What:** Replace bare `logging.basicConfig(level=INFO)` with formatted logs that include a request ID per API call.
- **Where:** `main.py:37-38`, plus a middleware.
- **Why:** When a stitch fails for a user, today there is no way to correlate the bot handler that received the original audio with the API calls that uploaded its recordings.
- **How:**
  1. `logging.basicConfig(level=INFO, format='%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s')` via a `LoggerAdapter` or `contextvars`.
  2. Middleware assigns `request["request_id"] = secrets.token_hex(4)` and binds it to the context.
  3. Bot handlers create their own request_id at entry.
- **Done when:** A failed stitch leaves a single search-able token in the logs that links the upload, the stitch call, and any error trace.

---

## Phase 5 — Mini App UX Polish

### 5.1 Preview-play each recording before stitching

- **What:** Add a play button to each item in the recordings list.
- **Where:** `webapp/index.html:874-897` (`_renderRecordings`), and a new API route to serve a single recording.
- **Why:** Today the only thing you can do with a recording is delete it (index.html:892). You can't tell whether the mic captured what you said until after stitching.
- **How:**
  1. New `GET /api/session/<id>/recording/<index>` returns the recording audio file with correct mime type. Reuse the same auth as 2.1.
  2. In `_renderRecordings`, add a play button next to delete. On click, instantiate an `Audio` element pointed at the new endpoint and toggle play/pause.
- **Done when:** Tapping a play icon plays just that comment without affecting the main player's position.

### 5.2 Telegram MainButton for "Stitch & Send"

- **What:** Use `Telegram.WebApp.MainButton` for the primary action instead of the custom green button.
- **Where:** `webapp/index.html:514` (current button) and `webapp/index.html:558-633` (`init` / setup).
- **Why:** Native button is pinned to the bottom of the Telegram viewport, looks consistent with other Mini Apps, and is always discoverable.
- **How:**
  1. `tg.MainButton.setText("Stitch & Send").show()` when there's ≥1 recording.
  2. `tg.MainButton.onClick(() => app.finish())`.
  3. Hide the in-page green button (or keep it as a fallback for non-Telegram browser previews — detect with `if (!tg.initData)`).
  4. `tg.MainButton.showProgress()` during stitching.
- **Done when:** On a real Telegram client, the "Stitch & Send" button is the system MainButton, not a custom DOM element.

### 5.3 Re-record over a comment

- **What:** A "re-record" action on each recording that deletes + immediately starts recording at the same timestamp.
- **Where:** `webapp/index.html:874-897` (`_renderRecordings`).
- **Why:** Today you have to delete, scrub back to the exact timestamp, then hit Record. Annoying.
- **How:**
  1. Add a `↻` button next to play/delete.
  2. On click, capture `rec.timestamp_ms`, call `deleteRecording(index)`, seek `this.audio.currentTime = rec.timestamp_ms / 1000`, then `startRecording()`.
- **Done when:** Tapping re-record on a comment at 0:42 starts a new recording at exactly 0:42 in one tap.

### 5.4 Audio uploading indicator in chat

- **What:** Send `bot.send_chat_action("upload_voice")` (or similar) while downloading + converting incoming audio.
- **Where:** `main.py:124-153` (top of `handle_audio_message`).
- **Why:** Users see "Processing audio..." text but no live indicator; on slow networks the UI looks frozen.
- **How:** `await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")` before download and conversion. Telegram clears it automatically after 5s, so re-call every few seconds inside a try/finally.
- **Done when:** While processing, the user sees the typing/recording indicator under the bot's name.

### 5.5 iOS Telegram in-app browser test pass

- **What:** Verify recording works in Telegram's WKWebView on iOS, and document the result.
- **Where:** N/A — this is a manual QA step.
- **Why:** README mentions iOS support "may vary" (line 164). MediaRecorder on iOS Safari 14.5+ exists but with quirks: `audio/mp4` recording works, `audio/webm` does not, and microphone permission must be re-granted per session.
- **How:** Walk through the comment flow on a real iPhone. If `audio/webm` is selected by the fallback chain (`index.html:767-776`), it will fail silently. Make sure `audio/mp4` is reached. Also test pause/resume during recording — iOS sometimes drops `ondataavailable` events on background.
- **Done when:** Comment flow works end-to-end on iOS Telegram with a recorded artifact (screenshots, version numbers) attached to a follow-up ticket.

---

## Phase 6 — Quality

### 6.1 Tests

- **What:** Pytest suite that exercises the stitching pipeline and the session store.
- **Where:** New `tests/` directory.
- **Why:** The stitching math (offsets, markers, parent-marker handling) is exactly the kind of code that regresses silently after refactors.
- **How:**
  1. `tests/test_audio_processor.py`:
     - Create a 10-second silent OGG fixture and a 1-second comment fixture.
     - Stitch with timestamps `[2000, 5000, 8000]`.
     - Assert `result_duration_ms == original + sum(insert_block_durations)`.
     - Assert markers are monotonically increasing and `end_ms > start_ms`.
  2. `tests/test_store.py`:
     - Concurrent `add_recording` calls (after 1.4) all land.
     - `remove_recording` deletes the file.
  3. `tests/test_api.py`:
     - aiohttp test client.
     - Auth: no header → 401; wrong user → 403; right user → 200.
- **Done when:** `pytest` runs green from a clean checkout (with ffmpeg installed in CI).

### 6.2 Lint / format

- **What:** Add `ruff` to `requirements.txt` (dev section or separate `requirements-dev.txt`) and a `pyproject.toml` config.
- **Why:** Catches dead imports (`urlencode`) and enforces a consistent style across the file.
- **Done when:** `ruff check telegram-audio-comments/` is clean.

### 6.3 Dockerfile that actually matches the README

- **What:** Promote the inline Dockerfile in `README.md:151-160` to a real file in the repo and add a `docker-compose.yml` for local dev.
- **Why:** Right now the Dockerfile is a snippet in markdown — easy to drift from the truth.
- **Done when:** `docker compose up` brings up the bot against an ngrok-tunneled hostname read from env.

---

## Phase 7 — Stretch: Comment Transcription

This is the highest-leverage UX feature but depends on every previous phase being stable.

### 7.1 Whisper transcription per comment

- **What:** Run speech-to-text on each uploaded recording and attach the transcript to the session.
- **Where:**
  - `main.py:423-482` (`api_upload_recording`): kick off transcription after duration is known.
  - `audio_processor.py` or new `transcriber.py`: the actual transcription call.
  - `store.py`: extend recording schema with `"transcript": "..." | None`.
- **Why:** A timeline of audio markers is opaque — you can't tell what each comment said without playing it. A short text snippet under each marker makes the conversation skim-able and searchable.
- **How:**
  1. Choose backend:
     - **OpenAI Whisper API** (`whisper-1`) — simplest, costs money, requires API key.
     - **`faster-whisper`** (local, CTranslate2) — free, requires GPU for speed but works on CPU for short clips. Good fit since recordings are 5–30s each.
     - **Telegram's own voice-to-text** — only available to Premium users via the client; not exposed via Bot API. Skip.
  2. Pick `faster-whisper` with `model="base"` for v1; small enough to run on CPU.
  3. After `add_recording` in `api_upload_recording`, schedule `asyncio.create_task(transcribe_recording(session_id, recording_filename))`.
  4. `transcribe_recording` runs `await asyncio.to_thread(model.transcribe, path, language=None)`, then `store.update_recording_transcript(session_id, filename, text)`.
  5. New `PATCH /api/session/<id>/recording/<idx>/transcript` is *not* needed — server-side only.
  6. The session GET endpoint (`main.py:359-381`) already returns `recordings`; include `transcript` in the per-recording dict at line 372.
- **Done when:** Uploading a recording where the user says "test one two three" results in `session.recordings[i].transcript ≈ "test one two three"` within a few seconds.

### 7.2 Show transcripts in the Mini App

- **What:** Display the transcript under each comment in the recordings list and the listen-mode timeline.
- **Where:** `webapp/index.html:874-897` (recordings list), `webapp/index.html:921-957` (`_renderMarkers`).
- **How:**
  1. In the recording list item, render a second line with the transcript (or "Transcribing…" if null).
  2. In listen mode, on hover/tap of a timeline marker, show a tooltip with the transcript.
  3. Poll the session endpoint while transcripts are pending (mirror the stitch-status polling pattern from 4.2).
- **Done when:** Every comment in the list shows either its transcript text or a "Transcribing…" placeholder, and the placeholder is replaced live as transcription completes.

### 7.3 Transcribe the stitched result and emit chapter markers

- **What:** Attach Telegram chapter markers (or a text caption block) to the stitched audio that the recipient can use to navigate.
- **Where:** `main.py:286-333` (`send_result`), depends on 7.1.
- **Why:** Recipients who don't use the Mini App still get a useful, scrollable transcript right in the chat.
- **How:**
  1. In the caption, list each comment as `1. 0:12 — "test one two three"`. Truncate to Telegram's caption limit (1024 chars); fall back to a follow-up text message if longer.
  2. Optional: also send a text `.srt`-style document for very long results.
- **Done when:** The "Done!" message after stitching includes a numbered transcript of all comments with timestamps.

---

## Execution Notes

- **Order matters.** Phase 1 fixes change the surface area that Phase 2 secures and Phase 7 builds on. Don't skip ahead.
- **One PR per numbered step.** Each step has explicit "Done when" criteria — use them as the PR checklist.
- **Track progress against this doc.** When a step is complete, check it off here so the doc stays the single source of truth for what's done vs. pending.
- **Re-test the full flow after each phase.** The end-to-end smoke test is: send a voice, comment three times at known timestamps, stitch, forward to another account, listen-skip, reply with one new comment. If that breaks at any point, stop and fix before moving on.
