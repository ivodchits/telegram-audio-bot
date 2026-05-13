# 🎙️ Telegram Audio Comments Bot

A Telegram bot that lets you add voice comments into audio messages at specific timestamps, creating a conversation-in-audio experience.

## How It Works

### The Comment Flow

1. **Your friend sends you a voice message** in any Telegram chat
2. **Forward it to this bot** (or send any audio directly)
3. **Tap "Comment on Audio"** — opens an in-app player (Telegram Mini App)
4. **Listen to the audio** — when you want to react to something, tap **Record**
5. **Playback pauses**, your microphone activates, and you record your comment
6. **Tap Stop** — your comment is saved at that exact timestamp
7. **Continue listening** and add as many comments as you want
8. **Tap "Stitch & Send"** — the bot merges your comments into the original audio
9. **Forward the result** to your friend

### The Listen Flow

When your friend receives the commented audio:

1. **Forward it back to the bot**
2. Choose **"Listen (Skip to Comments)"** — automatically jumps to each of your comments with a few seconds of context before each one
3. Or choose **"Full Listen"** — plays the entire combined audio straight through
4. **"Reply with Comments"** — adds their own comments on top, creating a back-and-forth audio conversation

### Audio Markers

When comments are inserted, you'll hear:
- **Double beep** (high tone) → a comment is about to play
- **Single beep** → comment ended, back to original audio

## Setup

### Prerequisites

- **Python 3.11+**
- **ffmpeg** (required for audio processing)
- A **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)
- **HTTPS endpoint** for the Mini App (use ngrok for development)

### Installation

```bash
# Clone and enter the project
cd telegram-audio-comments

# Install Python dependencies
pip install -r requirements.txt

# Install ffmpeg (if not already installed)
# Ubuntu/Debian:
sudo apt install ffmpeg
# macOS:
brew install ffmpeg

# Copy and edit environment config
cp .env.example .env
# Edit .env with your bot token and URL
```

### Configuration

Edit `.env`:

```env
BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
BASE_URL=https://your-domain.com
PORT=8080
CONTEXT_SECONDS=5
```

### Development with ngrok

Since Telegram Mini Apps require HTTPS:

```bash
# Terminal 1: Start ngrok
ngrok http 8080

# Copy the https URL (e.g., https://abc123.ngrok-free.app)
# Set it as BASE_URL in .env

# Terminal 2: Start the bot
python main.py
```

### Running

```bash
python main.py
```

The bot will start polling for messages and the web server will serve the Mini App.

## Architecture

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Telegram     │────▶│  Bot (aiogram)   │────▶│  Audio Processor │
│  User Chat    │◀────│  + Web Server    │◀────│  (pydub/ffmpeg)  │
└──────────────┘     │  (aiohttp)       │     └──────────────────┘
                      └────────┬────────┘
                               │
                      ┌────────▼────────┐
                      │  Mini App (HTML) │
                      │  Audio Player    │
                      │  + Recorder      │
                      └─────────────────┘
```

### Key Files

| File | Description |
|---|---|
| `main.py` | Entry point — bot handlers, API endpoints, web server |
| `audio_processor.py` | Audio stitching with pydub — merges comments into original |
| `store.py` | Session storage — JSON files on disk |
| `webapp/index.html` | Telegram Mini App — audio player + recorder UI |

### Data Flow

1. User sends audio → bot downloads to `data/audio/`
2. Session created in `data/sessions/`
3. Mini App loads audio via API, user records comments
4. Comments uploaded to `data/audio/` via API
5. Stitch API merges everything with pydub/ffmpeg
6. Result sent back as Telegram voice message

## Session Metadata

Each session tracks:
- **Original audio** file reference
- **Recordings** — list of `{timestamp_ms, filename, duration_ms}`
- **Result markers** — positions of inserted comments in the final audio `{start_ms, end_ms}`
- **Parent session** — for reply chains (commenting on already-commented audio)

## Deployment

For production deployment:

1. Use a proper HTTPS domain (not ngrok)
2. Set up a reverse proxy (nginx/caddy) in front of the app
3. Consider switching from file-based storage to a database
4. Add webhook mode instead of polling for better performance
5. Set up proper logging and monitoring

### Docker (optional)

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "main.py"]
```

## Limitations & Notes

- **Browser recording support**: Uses MediaRecorder API, which works in most modern mobile browsers and Telegram's in-app browser. iOS support may vary.
- **Audio format**: Telegram voice messages use OGG/Opus. Audio is converted to MP3 for web playback and back to OGG for sending.
- **File storage**: Sessions and audio files are stored on disk. For production, consider cloud storage (S3) and a database.
- **No authentication**: The API endpoints don't verify Telegram WebApp initData in this version. For production, enable the verification in `main.py`.
