# Discord Vault Bot

A Discord bot that gives you mobile/desktop access to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) through slash commands and free-text chat.

Chat with Claude, start remote coding sessions, capture notes to your Obsidian vault, and transcribe Instagram reels — all from Discord.

## Features

| Command | What it does |
|---------|-------------|
| `/help` | Show available commands |
| `/remote` | Start a Claude Code remote session and get the URL |
| `/remotestop` | Kill the active remote session |
| `/capture <text>` | Save a quick note to your vault inbox |
| `/reel <url>` | Transcribe an Instagram reel (requires yt-dlp + Whisper) |
| `/session_close` | Close the session (sweep decisions, delete RAM) |
| `/voice` | Join your voice channel — listen, transcribe, respond via TTS |
| `/voicestop` | Leave voice channel and extract transcript to Inbox |
| `/deafen` | Toggle whether the bot listens to your voice |
| `/restart` | Restart the bot process |
| `/shutdown` | Shut down the bot gracefully |
| *(free text)* | Send any message and Claude will respond |

Auto-detects Instagram URLs in free-text messages too.

The bot works in **text-only mode** by default. Voice commands require additional dependencies (see below).

## Prerequisites

- **Python 3.10–3.12** (3.13 removed `audioop` which voice depends on — use `pip install audioop-lts` if you must use 3.13)
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated (`claude` on PATH)
- **Discord account** with a server you control

Optional (for `/voice`):
- **[FFmpeg](https://ffmpeg.org/download.html)** on your PATH (required for TTS audio playback)
- **[Groq API key](https://console.groq.com)** (free tier, for cloud speech-to-text)
- Voice Python packages (installed via `requirements.txt` — see below)

Optional (for `/reel`):
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video downloader
- [openai-whisper](https://github.com/openai/whisper) — speech-to-text

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** tab:
   - Click **Reset Token** and copy it (you'll need this for `.env`)
   - Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. Go to **OAuth2** tab:
   - Under **Scopes**, check `bot` and `applications.commands`
   - Under **Bot Permissions**, check: Send Messages, Read Message History, Use Slash Commands
   - Copy the generated URL and open it in your browser to add the bot to your server

### 2. Get Your IDs

Enable **Developer Mode** in Discord (Settings → Advanced → Developer Mode), then:

- **Server ID**: Right-click your server name → Copy Server ID
- **User ID**: Right-click your profile → Copy User ID

### 3. Install Dependencies

**Text-only (chat, /capture, /remote):**
```bash
pip install discord.py python-dotenv edge-tts
```

**With voice support (/voice, /voicestop, /deafen):**
```bash
pip install -r requirements.txt
```
This installs all voice dependencies including PyTorch (~2GB download), Silero VAD, and Discord voice receive. You also need FFmpeg on your PATH.

**For Instagram transcription (/reel):**
```bash
pip install openai-whisper yt-dlp
```

### 4. Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_GUILD_ID=your-server-id
DISCORD_ALLOWED_USERS=your-user-id
CLAUDE_PROJECT_DIR=C:\path\to\your\claude\project
```

The only truly required paths are the three Discord values and `CLAUDE_PROJECT_DIR`. Everything else has sensible defaults.

### 5. Run

```bash
python bot.py
```

The bot will:
1. Kill any previous instance (PID file dedup)
2. Connect to Discord
3. Sync slash commands to your server (instant — guild-scoped)
4. Start listening for commands and messages

### Auto-Start on Windows

Copy `start-bot.bat` to your Startup folder:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
```

This launches the bot in the background (no console window) on login.

## Architecture

### Text Chat

Type any message in a channel the bot can see. The bot:
1. Builds a prompt with the last 10 messages of conversation history
2. Runs `claude -p --resume <session_id>` as a subprocess with multi-turn continuity
3. Returns Claude's response, chunked at 2000 chars (Discord's limit)

Text chat uses `--resume` for full conversation continuity across messages.

### Voice Pipeline

The voice system has a multi-stage pipeline with three layers of protection against false triggers:

```
Discord PCM (48kHz stereo)
    |
    v
Silero VAD (speech detection)
    |
    v
Deferred dispatch (cancel-safe)
    |
    v
Volume gate (-30 dBFS floor)
    |
    v
Groq Whisper STT (~0.5s)
    |
    v
Hallucination filter (blocklist + duration gate)
    |
    v
Utterance queue (if Claude is already working)
    |
    v
Claude Code (with vault access)
    |
    v
Edge TTS (paragraph-by-paragraph playback)
```

**Deferred cancellation**: New speech never kills in-flight Claude work until it passes all filters. Road noise that gets volume-rejected doesn't cancel your research. This is the key architectural difference from naive voice bots.

**Session memory**: Voice doesn't use `--resume`. Instead, the bot manages context directly with a three-tier model:

| Tier | What | Size |
|------|------|------|
| Session memory | Key facts extracted by Haiku after each turn | ~200-500 tokens |
| Recent turns | Last 8 turns verbatim (for tone/rapport) | ~500-2000 tokens |
| System context | Interrupt state, background tasks, channel history | ~100-300 tokens |

Total context per call stays flat at ~800-2800 tokens regardless of session length. No rotation, no context loss, no growing latency.

**Memory extraction**: After each Claude response, a lightweight Haiku call runs in parallel with TTS to extract key facts (decisions made, files examined, task progress). These accumulate as session memory and persist across bot restarts via `session-state.json`.

**Interruption handling**: Interrupt the bot mid-response. It tracks what was said vs. unsaid. Say "continue" to resume, or ask something new. Interrupts stack (FILO) — "pop stack" unwinds to a previous topic.

**Utterance queue**: If you speak while Claude is processing a request, your utterance is queued (not dropped). When Claude finishes, queued utterances are processed next. Say "cancel" or "stop" to kill in-flight work instead.

**Status queries**: Ask "are you there?" or "status" during a long Claude operation and the bot responds immediately with elapsed time, without interrupting the work.

**Background tasks**: Deep research or analysis can run in a separate Claude process while you continue conversing. Background results are added to session memory and announced when complete.

**Decision tracking**: The voice system prompt instructs Claude to write decisions to the Decision Log as they happen. This is how voice sessions communicate with desktop Claude Code sessions — both read/write the same Decision Log with a `source` column (`voice` or `desktop`).

**Session extraction**: On `/voicestop` or `/session_close`, the bot runs an extraction script against the Claude session's JSONL transcript. It produces a structured report (decisions, file modifications, topics discussed) saved to your inbox. The Discord text channel also contains the full conversation transcript.

### /remote

Spawns `claude remote-control` as a background process, reads stdout until it finds the session URL, and posts it to Discord. Use this to start a full Claude Code session from your phone.

### /capture

Creates a markdown file in your inbox folder with frontmatter and the captured text. Quick way to save thoughts or links.

### /reel (Optional)

Downloads an Instagram reel with yt-dlp, transcribes the audio with Whisper, and saves a vault note with metadata + transcript.

### Logs

The bot writes logs to a `logs/` directory (created automatically, gitignored). Each voice pipeline call is logged with timing breakdowns:

```
Voice: [pipeline] COMPLETE — STT=0.5s LLM=7.7s TTS=5.3s total=14.1s | spoke 1/1 chunks
```

## Configuration Reference

| Env Var | Required | Default | Description |
|---------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | — | Bot token from Developer Portal |
| `DISCORD_GUILD_ID` | Yes | — | Your Discord server ID |
| `DISCORD_ALLOWED_USERS` | Yes | — | Comma-separated user IDs |
| `CLAUDE_PROJECT_DIR` | No | Current directory | Where `claude -p` runs from |
| `VAULT_PATH` | No | `CLAUDE_PROJECT_DIR` | Obsidian vault root |
| `INBOX_PATH` | No | `VAULT_PATH/01_Inbox` | Where `/capture` saves notes |
| `SCRATCHPAD_DIR` | No | `CLAUDE_PROJECT_DIR/Scratchpad` | Session RAM files |
| `CLAUDE_MODEL` | No | `sonnet` | Claude model for main responses |
| `SYSTEM_PROMPT` | No | *(concise mode)* | System prompt for Claude subprocess |
| `YTDLP_PATH` | No | `yt-dlp` | Path to yt-dlp binary |
| `REEL_OUTPUT_DIR` | No | `VAULT_PATH/05_Reference/Instagram` | Where `/reel` saves transcripts |
| `FFMPEG_PATH` | No | `ffmpeg` | Path to FFmpeg binary |
| `GROQ_API_KEY` | No | — | Groq API key for speech-to-text |
| `TTS_VOICE` | No | `en-US-JennyNeural` | Edge TTS voice name |
| `TTS_RATE` | No | `+25%` | TTS speed adjustment |
| `VAD_SILENCE_TIMEOUT` | No | `2.5` | Seconds of silence before dispatching |
| `VAD_MIN_SPEECH_DURATION` | No | `0.5` | Minimum speech duration (seconds) |
| `VAD_SHORT_UTTERANCE_SECS` | No | `2.0` | Holdback threshold for short utterances |
| `VAD_HOLDBACK_WINDOW` | No | `3.0` | Seconds to wait for follow-on speech |
| `MIN_VOLUME_DBFS` | No | `-30` | dBFS floor for volume rejection |

## Security

- **Auth whitelist**: Only Discord user IDs in `DISCORD_ALLOWED_USERS` can interact with the bot. Non-whitelisted users are silently ignored.
- **Guild-scoped**: Slash commands only sync to your specific server, not globally.
- **PID file dedup**: Only one instance runs at a time. Starting a new one kills the old one.
- **No secrets in code**: All configuration is via environment variables.

## Troubleshooting

**Bot doesn't respond to messages**: Make sure you enabled **Message Content Intent** in the Discord Developer Portal (Bot → Privileged Gateway Intents).

**Slash commands don't appear**: They sync on bot startup. Try restarting the bot. Guild-scoped commands appear instantly (no 1-hour wait like global commands).

**"claude" not found**: Make sure Claude Code CLI is installed and on your PATH. Test with `claude --version`.

**Reel transcription fails**: Check that yt-dlp and whisper are installed. Instagram may block some downloads — yt-dlp is run with `--no-cookies-from-browser` to avoid account bans.

**Bot starts but no one can use it**: Check that your Discord User ID is in `DISCORD_ALLOWED_USERS`. It's a numeric ID, not your username.

**"/voice says dependencies not installed"**: Install voice packages: `pip install discord-ext-voice-recv groq davey torch silero-vad`. Make sure FFmpeg is on your PATH (`ffmpeg -version` should work).

**Voice joins but no audio / garbled audio**: Discord uses DAVE end-to-end encryption. The `davey` package handles decryption. If you see "DAVE decrypt failed" in logs, check that `davey` is the correct version for your `discord-ext-voice-recv` version.

**"No module named audioop"**: You're on Python 3.13+. Either downgrade to 3.12, or install the backport: `pip install audioop-lts`.
