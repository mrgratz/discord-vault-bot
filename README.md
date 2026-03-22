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
| `/session` | Create a session RAM file for Claude context tracking |
| `/session_close` | Close the session (sweep decisions, delete RAM) |
| `/voice` | Join your voice channel — listen, transcribe, respond via TTS |
| `/voicestop` | Leave voice channel and extract transcript to Inbox |
| `/deafen` | Toggle whether the bot listens to your voice |
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
cd tools/discord-bot
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

## How It Works

### Free-Text Chat

Type any message in a channel the bot can see. The bot:
1. Builds a prompt with the last 10 messages of conversation history
2. Runs `claude -p --model sonnet --append-system-prompt "..."` as a subprocess
3. Returns Claude's response, chunked at 2000 chars (Discord's limit)

### /remote

Spawns `claude remote-control` as a background process, reads stdout until it finds the session URL, and posts it to Discord. Use this to start a full Claude Code session from your phone.

### /capture

Creates a markdown file in your inbox folder with frontmatter and the captured text. Quick way to save thoughts or links.

### /reel (Optional)

Downloads an Instagram reel with yt-dlp, transcribes the audio with Whisper, and saves a vault note with metadata + transcript.

### /session

Creates a `session-{N}-ram.md` file for tracking what Claude does across multiple interactions. When active, the bot injects session context into Claude's system prompt. `/session_close` asks Claude to sweep for decisions before deleting the RAM file.

### /voice — Voice Pipeline

The voice system is the most complex feature. Here's how it works:

1. **Listen**: Discord sends 20ms PCM audio frames. Silero VAD detects speech onset/offset.
2. **Buffer**: Speech is buffered until silence is detected (2.5s timeout). Short utterances (<2s) are held briefly to merge fragments from natural pauses.
3. **Transcribe**: PCM is converted to WAV and sent to Groq's Whisper API for cloud STT (~0.5s).
4. **Think**: Transcript is sent to `claude -p --resume` with conversation context. Claude has access to your vault via CLAUDE.md.
5. **Speak**: Response is chunked into paragraphs, synthesized via edge-tts, and played back through Discord.

**Interruption handling**: You can interrupt the bot mid-response. It tracks what was said vs. unsaid. Say "continue" to resume, or ask something new. Interrupts stack — "pop stack" unwinds to a previous topic.

**Session rotation**: After every 7 voice turns, the Claude session is dropped and restarted fresh. This prevents context accumulation from slowing down responses (a 20-turn session can take 3x longer per response). Discord channel history is injected as context for the new session, so continuity is preserved.

**Decision tracking**: The voice system prompt instructs Claude to write decisions to the Decision Log as they happen. This is how voice sessions communicate with desktop Claude Code sessions — both read/write the same Decision Log with a `source` column (`voice` or `desktop`).

**Session extraction**: On `/voicestop` or `/session_close`, the bot runs an extraction script against the Claude session's JSONL transcript. It produces a structured report (decisions, file modifications, topics discussed) saved to your inbox. The Discord text channel also contains the full conversation transcript (every user message and bot response is posted there).

### Logs

The bot writes logs to a `logs/` directory (created automatically, gitignored). The main log file is `logs/bot.log`. Each voice pipeline call is logged with timing breakdowns:

```
Voice: [pipeline] COMPLETE — STT=0.5s LLM=7.7s TTS=5.3s total=14.1s | spoke 1/1 chunks
```

This helps diagnose latency issues — STT and TTS are usually fast, LLM time is where session bloat shows up.

## Configuration Reference

| Env Var | Required | Default | Description |
|---------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | — | Bot token from Developer Portal |
| `DISCORD_GUILD_ID` | Yes | — | Your Discord server ID |
| `DISCORD_ALLOWED_USERS` | Yes | — | Comma-separated user IDs |
| `CLAUDE_PROJECT_DIR` | No | Current directory | Where `claude -p` runs from |
| `VAULT_PATH` | No | `CLAUDE_PROJECT_DIR` | Obsidian vault root |
| `INBOX_PATH` | No | `VAULT_PATH/01_Inbox` | Where `/capture` saves notes |
| `SCRATCHPAD_DIR` | No | `CLAUDE_PROJECT_DIR/Scratchpad` | Where `/session` creates files |
| `CLAUDE_MODEL` | No | `sonnet` | Claude model (haiku/sonnet/opus) |
| `SYSTEM_PROMPT` | No | *(concise mode)* | System prompt for Claude subprocess |
| `YTDLP_PATH` | No | `yt-dlp` | Path to yt-dlp binary |
| `REEL_OUTPUT_DIR` | No | `VAULT_PATH/05_Reference/Instagram` | Where `/reel` saves transcripts |
| `FFMPEG_PATH` | No | `ffmpeg` | Path to FFmpeg binary (voice TTS playback) |
| `GROQ_API_KEY` | No | — | Groq API key for cloud speech-to-text |
| `TTS_VOICE` | No | `en-US-JennyNeural` | Edge TTS voice name |
| `TTS_RATE` | No | `+25%` | TTS speed adjustment |
| `VAD_SILENCE_TIMEOUT` | No | `2.5` | Seconds of silence before dispatching |
| `VAD_MIN_SPEECH_DURATION` | No | `0.5` | Minimum speech duration (seconds) |
| `VAD_SHORT_UTTERANCE_SECS` | No | `2.0` | Holdback threshold for short utterances |
| `VAD_HOLDBACK_WINDOW` | No | `3.0` | Seconds to wait for follow-on speech |
| `MIN_VOLUME_DBFS` | No | `-30` | dBFS floor for volume rejection |
| `VOICE_ROTATION_TURNS` | No | `7` | Rotate Claude session every N turns |

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
