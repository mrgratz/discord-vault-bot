"""
Discord Vault Bot — Access Claude Code from Discord.

Gives you a Discord interface to Claude Code with:
- Free-text chat (sends messages to `claude -p` subprocess)
- /remote to spawn a Claude Code remote-control session
- /capture to save quick notes to your vault inbox
- /reel to transcribe Instagram reels via yt-dlp + Whisper
- /voice to join a voice channel, transcribe speech, respond via TTS
- /session to manage Claude Code session RAM files

Requirements: Python 3.10+, discord.py[voice]>=2.0, discord-ext-voice-recv,
edge-tts, Claude Code CLI (`claude`), FFmpeg.
Optional: yt-dlp + openai-whisper (for /reel and /voice), python-dotenv.

See README.md for full setup instructions.
"""

import asyncio
import io
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import discord
import edge_tts
from discord import app_commands

# --- Voice dependencies (optional — bot runs text-only without these) ---
VOICE_AVAILABLE = False
try:
    import audioop
    from discord.ext import voice_recv
    from groq import Groq
    import davey
    import torch
    from silero_vad import load_silero_vad
    import discord.ext.voice_recv.router as _voice_router
    import discord.ext.voice_recv.opus as _voice_opus
    VOICE_AVAILABLE = True
except ImportError as _voice_import_err:
    Groq = None  # type: ignore
    _voice_import_err_msg = str(_voice_import_err)

if VOICE_AVAILABLE:
    # --- DAVE E2EE decryption for voice receive ---
    # discord-ext-voice-recv only does AEAD transport decryption.
    # Discord now requires DAVE (end-to-end encryption) on all voice.
    # We patch _process_packet to call dave_session.decrypt() before opus decode.
    _original_process_packet = _voice_opus.PacketDecoder._process_packet
    _dave_decrypt_stats = {"ok": 0, "fail": 0, "logged_ok": False}

    def _patched_process_packet(self, packet):
        vc = self.sink.voice_client
        conn = getattr(vc, '_connection', None) if vc else None
        dave_session = getattr(conn, 'dave_session', None) if conn else None

        if dave_session and self._cached_id and packet.decrypted_data:
            try:
                decrypted = dave_session.decrypt(
                    self._cached_id, davey.MediaType.audio, packet.decrypted_data
                )
                if decrypted is not None:
                    packet.decrypted_data = bytes(decrypted)
                    _dave_decrypt_stats["ok"] += 1
                    if not _dave_decrypt_stats["logged_ok"]:
                        _dave_decrypt_stats["logged_ok"] = True
                        log.info("DAVE decrypt OK: %d -> %d bytes, opus_toc=0x%02x",
                                 len(packet.decrypted_data), len(decrypted),
                                 decrypted[0] if decrypted else 0)
            except Exception as e:
                _dave_decrypt_stats["fail"] += 1
                # Log every 50th failure to avoid spam but keep visibility
                if _dave_decrypt_stats["fail"] <= 3 or _dave_decrypt_stats["fail"] % 50 == 0:
                    log.warning("DAVE decrypt failed #%d (user=%s): %s",
                                _dave_decrypt_stats["fail"], self._cached_id, e)

        return _original_process_packet(self, packet)

    _voice_opus.PacketDecoder._process_packet = _patched_process_packet

    # Monkey-patch: protect router loop from opus decode crashes
    def _patched_do_run(self):
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    try:
                        data = decoder.pop_data()
                    except Exception:
                        continue
                    if data is not None:
                        try:
                            self.sink.write(data.source, data)
                        except Exception:
                            log.exception("Error in sink.write() — router thread surviving")

    _voice_router.PacketRouter._do_run = _patched_do_run
else:
    _voice_unavail_reason = _voice_import_err_msg  # logged after logger is set up

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional — set env vars directly if you prefer

# --- Required configuration ---
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
ALLOWED_USERS = {int(uid) for uid in os.environ["DISCORD_ALLOWED_USERS"].split(",")}

# --- Paths (all configurable via env vars) ---
# CLAUDE_PROJECT_DIR: where `claude -p` runs from. Should contain your CLAUDE.md.
CLAUDE_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
# VAULT_PATH: root of your Obsidian vault (used by /capture and /reel)
VAULT_PATH = Path(os.environ.get("VAULT_PATH", CLAUDE_PROJECT_DIR))
# INBOX_PATH: where /capture saves notes. Set to any folder you like.
INBOX_PATH = Path(os.environ.get("INBOX_PATH", str(VAULT_PATH / "01_Inbox")))

LOG_DIR = Path(__file__).parent / "logs"
PID_FILE = Path(__file__).parent / "bot.pid"
SESSION_STATE_FILE = Path(__file__).parent / "session-state.json"
HISTORY_LENGTH = 10
VOICE_ROTATION_TURNS = 7  # rotate voice session every N turns to prevent context bloat

# --- Optional: Reel transcription (requires yt-dlp + whisper) ---
YTDLP_PATH = os.environ.get("YTDLP_PATH", "yt-dlp")
REEL_OUTPUT_DIR = Path(os.environ.get("REEL_OUTPUT_DIR", str(VAULT_PATH / "05_Reference" / "Instagram")))
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)"
)

# --- Claude model (configurable — "sonnet" is fast + cheap, "opus" for complex tasks) ---
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")

# --- Voice configuration ---
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-JennyNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+25%")  # speed up TTS (e.g. "+25%", "+50%", "-10%")
VAD_SPEECH_THRESHOLD = float(os.environ.get("VAD_SPEECH_THRESHOLD", "0.5"))
NO_SPEECH_PROB_THRESHOLD = float(os.environ.get("NO_SPEECH_PROB_THRESHOLD", "0.5"))
MIN_VOLUME_DBFS = float(os.environ.get("MIN_VOLUME_DBFS", "-30"))
VAD_SILENCE_TIMEOUT = float(os.environ.get("VAD_SILENCE_TIMEOUT", "2.5"))
VAD_MIN_SPEECH_DURATION = float(os.environ.get("VAD_MIN_SPEECH_DURATION", "0.5"))
# Consecutive speech frames to start buffering (5 = 160ms at 32ms/frame)
VAD_CONFIRM_FRAMES = int(os.environ.get("VAD_CONFIRM_FRAMES", "5"))
# Consecutive speech frames to interrupt TTS (10 = 320ms sustained speech)
VAD_INTERRUPT_FRAMES = int(os.environ.get("VAD_INTERRUPT_FRAMES", "10"))
# Seconds of sustained confirmed speech required before interrupting TTS
VAD_INTERRUPT_DELAY = float(os.environ.get("VAD_INTERRUPT_DELAY", "0.8"))
# Short utterance holdback: utterances shorter than this wait for continuation before dispatching
VAD_SHORT_UTTERANCE_SECS = float(os.environ.get("VAD_SHORT_UTTERANCE_SECS", "2.0"))
# How long to wait for continuation speech before dispatching a held short utterance
VAD_HOLDBACK_WINDOW = float(os.environ.get("VAD_HOLDBACK_WINDOW", "3.0"))

# --- Groq cloud STT (replaces local Whisper for voice) ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
_groq_client: Groq | None = None

# Known Whisper hallucination phrases (produced when given noise/silence)
WHISPER_HALLUCINATIONS = {
    "thank you.", "thank you", "thanks for watching.", "thanks for watching!",
    "thanks for watching", "thank you for watching.", "thank you for watching",
    "please subscribe.", "like and subscribe.", "subscribe",
    "you", "",
}

def _get_groq_client() -> Groq | None:
    global _groq_client
    if _groq_client is None and GROQ_API_KEY:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client

REMOTE_URL_RE = re.compile(r"https://claude\.ai/code/session_\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")

SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", (
    "You are responding to a short Discord message. "
    "Be concise — aim for 1-3 short paragraphs max. "
    "Do not ask for permission to read files — just read them."
))

# Logging setup
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("vault-bot")
if not VOICE_AVAILABLE:
    log.warning("Voice dependencies not available: %s — /voice command disabled", _voice_unavail_reason)
else:
    # Suppress noisy RTCP spam from voice_recv (1 per second)
    logging.getLogger("discord.ext.voice_recv").setLevel(logging.WARNING)

# Per-channel conversation history (in-memory, fallback when --resume fails)
chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LENGTH))

# Dedup guard for on_message (Discord can send duplicate events after reconnect)
_seen_message_ids: deque = deque(maxlen=100)
# Lock to prevent concurrent text chat Claude calls (race condition on session ID)
_text_chat_lock: asyncio.Lock | None = None  # created in on_ready

# Active remote-control process (one at a time)
_remote_proc: asyncio.subprocess.Process | None = None

# Persistent session IDs (text + voice, separate contexts)
_text_session_id: str | None = None
_voice_session_id: str | None = None  # also stored in _voice_states per guild


# --- Session state persistence ---

def _load_session_state():
    """Load persisted session IDs from disk."""
    global _text_session_id, _voice_session_id
    if SESSION_STATE_FILE.exists():
        try:
            data = json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
            _text_session_id = data.get("text_session_id")
            _voice_session_id = data.get("voice_session_id")
            log.info("Loaded session state: text=%s, voice=%s",
                     _text_session_id[:12] + "..." if _text_session_id else "none",
                     _voice_session_id[:12] + "..." if _voice_session_id else "none")
        except Exception as e:
            log.warning("Failed to load session state: %s", e)


def _save_session_state():
    """Persist session IDs to disk."""
    data = {
        "text_session_id": _text_session_id,
        "voice_session_id": _voice_session_id,
        "last_updated": datetime.now().isoformat(),
    }
    try:
        SESSION_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to save session state: %s", e)

# --- Voice context (Discord channel history replaces file-based RAM) ---

async def _fetch_channel_context(guild_id: int, limit: int = 20) -> str:
    """Fetch recent Discord text channel messages as conversation context.

    Returns formatted string of recent messages, or empty string if unavailable.
    This replaces file-based voice RAM — the Discord channel IS the memory.
    """
    text_channel_id = _voice_text_channels.get(guild_id)
    if not text_channel_id:
        return ""
    channel = bot.get_channel(text_channel_id)
    if not channel:
        return ""
    try:
        messages = [msg async for msg in channel.history(limit=limit)]
        messages.reverse()  # chronological order
        lines = []
        for msg in messages:
            if msg.author.bot and msg.content.startswith("> "):
                # Bot response (quoted)
                lines.append(f"Assistant: {msg.content[2:][:200]}")
            elif msg.author.bot and msg.content.startswith("*Rotating"):
                continue  # skip rotation notices
            elif not msg.author.bot:
                # User transcript (format: **displayname:** text)
                text = msg.content
                if text.startswith("**") and ":**" in text:
                    text = text.split(":**", 1)[1].strip()
                lines.append(f"User: {text[:200]}")
        context = "\n".join(lines[-30:])  # cap at 30 lines
        return context
    except Exception as e:
        log.warning("Failed to fetch channel context: %s", e)
        return ""


async def _rotate_voice_session(guild_id: int) -> None:
    """Rotate the voice session to prevent context bloat.

    Drops the session ID so the next call starts fresh. Discord channel
    history provides continuity — no file-based RAM needed.
    """
    global _voice_session_id
    vs = _get_voice_state(guild_id)

    old_sid = _voice_session_id
    _voice_session_id = None
    _save_session_state()
    vs["turn_count"] = 0
    vs["history"].clear()

    log.info("Voice: session rotated (was %s, turn_count reset, context from channel history)",
             old_sid[:12] if old_sid else "none")


# Voice state per guild
_voice_text_channels: dict[int, int] = {}
_voice_locks: dict[int, asyncio.Lock] = {}
_voice_interrupted: dict[int, bool] = {}  # set by sink when user interrupts playback
_voice_pipeline_task: dict[int, asyncio.Task] = {}  # active pipeline per guild — cancelled on new utterance
_persistent_source: dict[int, "PersistentAudioSource"] = {}  # one per guild, plays forever
_voice_deafened: set[int] = set()  # user IDs whose voice input is ignored

VOICE_HISTORY_LENGTH = 30


def _get_voice_state(guild_id: int) -> dict:
    """Get or create per-guild voice conversation state."""
    if guild_id not in _voice_states:
        _voice_states[guild_id] = {
            "history": deque(maxlen=VOICE_HISTORY_LENGTH),
            "pending_chunks": [],   # paragraphs not yet spoken (current response)
            "spoken_chunks": [],    # paragraphs already spoken (current response)
            "full_response": "",    # complete last Claude response
            "interrupted": False,   # was the last response interrupted?
            # Stack of interrupted contexts — survives intervening questions.
            # Each entry: {"spoken": [...], "pending": [...]}
            # Push on interrupt, pop on "continue". Supports cascading branches.
            "interrupt_stack": [],
            "turn_count": 0,        # turns since last session rotation
        }
    return _voice_states[guild_id]


_voice_states: dict[int, dict] = {}

VOICE_SYSTEM_PROMPT = (
    "You are in a live voice conversation via Discord. The user speaks, you respond "
    "with speech (text-to-speech, read aloud paragraph by paragraph).\n\n"
    "Conversation model:\n"
    "- Your responses are read aloud one paragraph at a time. Use paragraph breaks to "
    "create natural pauses.\n"
    "- The user can interrupt you mid-response. If they ask a question or give a command, "
    "handle it. They may then say 'carry on', 'continue', 'go on', etc. to resume.\n"
    "- When the user asks to resume after an interruption, you'll receive context about "
    "what you already said and what remained unsaid. Pick up naturally from where you "
    "left off — don't repeat what was already spoken.\n"
    "- The user can give commands mid-conversation: capturing notes, reading files, etc. "
    "Handle these normally, then be ready to resume the prior topic if asked.\n"
    "- If the user says 'skip' or 'skip paragraph', they want you to move past the "
    "current section. Acknowledge briefly and continue with the next point.\n\n"
    "CRITICAL: Be extremely concise. Every sentence costs 5-10 seconds of audio.\n"
    "- Simple questions: 1-2 sentences max.\n"
    "- Explanations: hit the key points, skip preamble and filler. No 'Great question!' "
    "or 'That's a good point.' Just answer.\n"
    "- Use short, direct sentences. Conversational, not formal.\n"
    "- Organize longer responses into clear paragraphs for natural pauses.\n\n"
    "Decision tracking:\n"
    "- When the user makes a decision, states a preference, or settles a question during "
    "conversation, write it to the Decision Log immediately using Edit tool.\n"
    "- Format: add a row to the strategic decisions table in the Decision Log with "
    "source 'voice' and today's date.\n"
    "- This is how voice sessions communicate with desktop sessions. If you don't write "
    "it, the desktop session won't know it happened.\n\n"
    "File reading:\n"
    "- Only read files when the user explicitly asks about vault contents, roadmaps, "
    "or specific files. Do not speculatively read files to orient yourself on simple "
    "questions or greetings."
)


# --- PID file management ---

def kill_existing_instance():
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        # os.kill(SIGTERM) doesn't reliably kill Python on Windows.
        # Use taskkill /F which works for both python.exe and pythonw.exe.
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)],
                           capture_output=True, timeout=5)
        else:
            os.kill(old_pid, signal.SIGTERM)
        log.info("Killed previous instance (PID %d)", old_pid)
    except (ValueError, ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        pass
    PID_FILE.unlink(missing_ok=True)


def write_pid():
    PID_FILE.write_text(str(os.getpid()))


def cleanup_pid():
    PID_FILE.unlink(missing_ok=True)


# --- Auth ---

def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


# --- Message utilities ---

def split_message(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def build_prompt(message: str, channel_id: int) -> str:
    history = chat_history[channel_id]
    if not history:
        return message
    lines = []
    for role, text in history:
        lines.append(f"{role}: {text}")
    return (
        "Previous messages in this conversation:\n"
        + "\n".join(lines)
        + f"\n\nUser: {message}"
    )


# --- Claude subprocess ---

async def run_claude(message: str, extra_system: str = "", session_id: str | None = None) -> tuple[str, str | None]:
    """Run claude -p subprocess. Returns (response_text, session_id).

    If session_id is provided, resumes that session (--resume).
    Always uses --output-format json to capture session_id for future calls.
    """
    preview = message[:80].replace("\n", " ")
    log.info("Claude request: %s%s%s", preview, "..." if len(message) > 80 else "",
             f" (resume={session_id[:12]}...)" if session_id else "")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    system = SYSTEM_PROMPT
    if extra_system:
        system += "\n\n" + extra_system

    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "json",
           "--append-system-prompt", system]
    if session_id:
        cmd.extend(["--resume", session_id])

    t0 = datetime.now()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=CLAUDE_PROJECT_DIR,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(message.encode()), timeout=300)
    except asyncio.TimeoutError:
        elapsed = (datetime.now() - t0).total_seconds()
        log.error("Timed out after %.0fs. Killing process.", elapsed)
        proc.kill()
        return "Timed out after 300s.", None
    elapsed = (datetime.now() - t0).total_seconds()
    stderr_text = stderr.decode().strip()
    if stderr_text:
        log.warning("Claude stderr (%.0fs): %s", elapsed, stderr_text[:500])
    if proc.returncode != 0:
        log.error("Claude failed (exit %d, %.0fs): %s", proc.returncode, elapsed, stderr_text[:500])
        # If resume failed, retry without resume (session may have expired)
        if session_id:
            log.info("Retrying without --resume (session may have expired)")
            return await run_claude(message, extra_system=extra_system, session_id=None)
        return f"Error (exit {proc.returncode}): {stderr_text[:500]}", None

    raw = stdout.decode().strip()
    # Parse JSON output for response text and session_id
    new_session_id = None
    response = raw or "(empty response)"
    try:
        data = json.loads(raw)
        response = data.get("result", raw) or "(empty response)"
        new_session_id = data.get("session_id")
    except (json.JSONDecodeError, TypeError):
        # Fallback: treat as plain text (shouldn't happen with --output-format json)
        log.warning("Claude output was not JSON, using raw text")

    log.info("Claude responded (%.0fs, %d chars, session=%s)", elapsed, len(response),
             new_session_id[:12] + "..." if new_session_id else "none")
    return response, new_session_id


# --- Reel transcription ---

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
        except ImportError:
            raise RuntimeError(
                "openai-whisper is not installed. Install it with: pip install openai-whisper"
            )
        log.info("Loading Whisper model (first use)...")
        _whisper_model = whisper.load_model("small")
        log.info("Whisper model loaded.")
    return _whisper_model


async def download_reel(url: str, tmpdir: str) -> tuple[Path, dict]:
    cmd = [
        YTDLP_PATH,
        "--no-playlist",
        "--write-info-json",
        "--no-cookies-from-browser",
        "-o", os.path.join(tmpdir, "%(id)s.%(ext)s"),
        url,
    ]
    log.info("yt-dlp downloading: %s", url)
    t0 = datetime.now()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("yt-dlp timed out after 120s")
    elapsed = (datetime.now() - t0).total_seconds()
    if proc.returncode != 0:
        err = stderr.decode().strip()[:500]
        raise RuntimeError(f"yt-dlp failed (exit {proc.returncode}): {err}")
    log.info("yt-dlp finished (%.0fs)", elapsed)

    tmppath = Path(tmpdir)
    videos = list(tmppath.glob("*.mp4")) + list(tmppath.glob("*.webm"))
    if not videos:
        raise RuntimeError("yt-dlp produced no video file")
    video_path = videos[0]

    info_files = list(tmppath.glob("*.info.json"))
    metadata = {}
    if info_files:
        metadata = json.loads(info_files[0].read_text(encoding="utf-8"))

    return video_path, metadata


async def transcribe_audio(video_path: Path) -> str:
    loop = asyncio.get_event_loop()
    model = _get_whisper_model()

    def _transcribe():
        result = model.transcribe(
            str(video_path), fp16=False, language="en",
            # Discord voice audio quality triggers Whisper's VAD at ~0.65 no_speech_prob.
            # Default threshold (0.6) filters it out. Relax to 0.9 for voice chat.
            no_speech_threshold=0.9,
            logprob_threshold=-1.5,
        )
        return result["text"].strip()

    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _transcribe),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise TimeoutError("Whisper transcription timed out after 120s")
    return text


async def transcribe_audio_groq(audio_data: bytes, fmt: str = "wav") -> tuple[str, float]:
    """Transcribe audio via Groq's Whisper API (cloud, fast, accurate).
    Returns (transcript_text, max_no_speech_prob)."""
    client = _get_groq_client()
    if client is None:
        raise RuntimeError("GROQ_API_KEY not set")
    loop = asyncio.get_event_loop()
    filename = f"audio.{fmt}"

    def _call_groq():
        return client.audio.transcriptions.create(
            file=(filename, audio_data),
            model="whisper-large-v3-turbo",
            language="en",
            response_format="verbose_json",
        )

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _call_groq),
            timeout=15,
        )
    except asyncio.TimeoutError:
        raise TimeoutError("Groq transcription timed out after 15s")

    no_speech_prob = 0.0
    if isinstance(result, str):
        text = result.strip()
    else:
        text = (result.text or "").strip()
        segments = getattr(result, "segments", None) or []
        if segments:
            no_speech_prob = max(
                getattr(seg, "no_speech_prob", 0.0) or 0.0
                for seg in segments
            )
    return text, no_speech_prob


def build_vault_note(metadata: dict, transcript: str, url: str) -> tuple[str, str]:
    creator = metadata.get("uploader") or metadata.get("channel") or "unknown"
    shortcode = metadata.get("id") or "unknown"
    caption = metadata.get("description") or ""
    upload_date = metadata.get("upload_date") or ""
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    duration = metadata.get("duration")
    duration_str = f"{int(duration)}s" if duration else ""
    likes = metadata.get("like_count")
    comments = metadata.get("comment_count")
    today = datetime.now().strftime("%Y-%m-%d")

    hashtags = re.findall(r"#\w+", caption) if caption else []

    fm_lines = [
        "---",
        f"created: {today}",
        f"type: reel-transcript",
        f"source: {url}",
        f"creator: {creator}",
    ]
    if upload_date:
        fm_lines.append(f"upload_date: {upload_date}")
    if duration_str:
        fm_lines.append(f"duration: {duration_str}")
    if likes is not None:
        fm_lines.append(f"likes: {likes}")
    if comments is not None:
        fm_lines.append(f"comments: {comments}")
    if hashtags:
        fm_lines.append(f"tags: [{', '.join(hashtags)}]")
    fm_lines.append("---")

    body_parts = [f"# {creator} - {shortcode}\n"]
    if caption:
        body_parts.append(f"## Caption\n\n{caption}\n")
    body_parts.append(f"## Transcript\n\n{transcript}\n")

    content = "\n".join(fm_lines) + "\n\n" + "\n".join(body_parts)

    safe_creator = re.sub(r'[<>:"/\\|?*]', "", creator)[:50]
    filename = f"{safe_creator} - {shortcode}.md"

    return filename, content


async def handle_reel(interaction: discord.Interaction, url: str) -> None:
    match = INSTAGRAM_URL_RE.search(url)
    shortcode = match.group(1) if match else "unknown"
    log.info("Reel request: %s (shortcode: %s)", url, shortcode)

    await interaction.followup.send(f"Processing reel {shortcode}...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                video_path, metadata = await download_reel(url, tmpdir)
            except TimeoutError:
                await interaction.followup.send("Download timed out (120s limit).")
                return
            except RuntimeError as e:
                log.error("Download failed: %s", e)
                await interaction.followup.send(f"Download failed: {e}")
                return

            try:
                transcript = await transcribe_audio(video_path)
            except TimeoutError:
                await interaction.followup.send("Transcription timed out (120s limit).")
                return
            except Exception as e:
                log.error("Transcription failed: %s", e)
                await interaction.followup.send(f"Transcription failed: {e}")
                return

        if not transcript:
            await interaction.followup.send("No speech detected in this reel.")
            return

        filename, content = build_vault_note(metadata, transcript, url)
        REEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = REEL_OUTPUT_DIR / filename
        filepath.write_text(content, encoding="utf-8")
        log.info("Saved reel transcript: %s", filepath)

        reply = f"Transcript saved to `05_Reference/Instagram/{filename}`\n\n{transcript}"
        for chunk in split_message(reply):
            await interaction.followup.send(chunk)

    except Exception as e:
        log.error("Reel pipeline error: %s", e, exc_info=True)
        await interaction.followup.send(f"Error processing reel: {e}")


async def handle_reel_from_message(message: discord.Message, url: str) -> None:
    """Handle reel from free-text message (not slash command)."""
    match = INSTAGRAM_URL_RE.search(url)
    shortcode = match.group(1) if match else "unknown"
    log.info("Reel request (auto-detect): %s (shortcode: %s)", url, shortcode)

    await message.reply(f"Processing reel {shortcode}...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                video_path, metadata = await download_reel(url, tmpdir)
            except TimeoutError:
                await message.reply("Download timed out (120s limit).")
                return
            except RuntimeError as e:
                log.error("Download failed: %s", e)
                await message.reply(f"Download failed: {e}")
                return

            try:
                transcript = await transcribe_audio(video_path)
            except TimeoutError:
                await message.reply("Transcription timed out (120s limit).")
                return
            except Exception as e:
                log.error("Transcription failed: %s", e)
                await message.reply(f"Transcription failed: {e}")
                return

        if not transcript:
            await message.reply("No speech detected in this reel.")
            return

        filename, content = build_vault_note(metadata, transcript, url)
        REEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = REEL_OUTPUT_DIR / filename
        filepath.write_text(content, encoding="utf-8")
        log.info("Saved reel transcript: %s", filepath)

        reply = f"Transcript saved to `05_Reference/Instagram/{filename}`\n\n{transcript}"
        for chunk in split_message(reply):
            await message.reply(chunk)

    except Exception as e:
        log.error("Reel pipeline error: %s", e, exc_info=True)
        await message.reply(f"Error processing reel: {e}")


# --- Session extraction ---

async def _extract_session_to_inbox(session_id: str | None, label: str) -> str:
    """Extract a Claude session transcript to an Inbox note.

    Returns a user-facing status message.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%d %H%M%S")
    note_file = INBOX_PATH / f"{label.title()} Session {timestamp}.md"
    INBOX_PATH.mkdir(parents=True, exist_ok=True)

    if not session_id:
        return f"*(No active {label} session to extract.)*"

    report_text = None
    extract_script = Path(CLAUDE_PROJECT_DIR) / "tools" / "extract-session-report.py"
    tmp_out = Path(tempfile.gettempdir()) / f"{label}-extract-{session_id[:8]}.md"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(extract_script), "--id", session_id[:8], "--out", str(tmp_out),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(CLAUDE_PROJECT_DIR),
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if tmp_out.exists():
            report_text = tmp_out.read_text(encoding="utf-8")
            tmp_out.unlink(missing_ok=True)
    except Exception as e:
        log.warning("%s extract failed: %s", label, e)

    if report_text:
        content = (
            f"---\ncreated: {today}\ntype: {label}-session\nsource: discord-bot\n"
            f"session_id: {session_id}\n---\n\n"
            f"# {label.title()} Session — {today}\n\n"
            f"{report_text}\n"
        )
        note_file.write_text(content, encoding="utf-8")
        log.info("%s extract: session %s → %s", label, session_id[:12], note_file.name)
        return f"{label.title()} session extracted → `01_Inbox/{note_file.name}`"
    else:
        content = (
            f"---\ncreated: {today}\ntype: {label}-session\nsource: discord-bot\n"
            f"session_id: {session_id}\n---\n\n"
            f"# {label.title()} Session — {today}\n\n"
            f"Session ID: `{session_id}`\n\n"
            f"JSONL extract unavailable — use `extract-session-report.py --id {session_id[:8]}` to recover.\n"
        )
        note_file.write_text(content, encoding="utf-8")
        log.info("%s extract: fallback note → %s", label, note_file.name)
        return f"{label.title()} session note saved → `01_Inbox/{note_file.name}` *(JSONL extract failed)*"


# --- Voice: PersistentAudioSource ---

class PersistentAudioSource(discord.AudioSource):
    """Audio source that plays forever — TTS content or silence.

    Solves Discord voice dormancy: outbound audio never stops, so Discord
    never stops routing inbound user audio. Replaces the keepalive system.

    Usage:
        source = PersistentAudioSource()
        vc.play(source)           # Starts playing silence
        source.set_source(ffmpeg) # Switch to TTS audio
        # ... source auto-reverts to silence when FFmpeg exhausts
        source.interrupt()        # Force-switch to silence mid-TTS
    """

    # 3-byte Opus silence frame — Discord accepts this as valid audio
    OPUS_SILENCE = b'\xf8\xff\xfe'

    def __init__(self):
        self._lock = threading.Lock()
        self._source: discord.AudioSource | None = None
        self._done_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_source(self, source: discord.AudioSource, done_event: asyncio.Event,
                   loop: asyncio.AbstractEventLoop) -> None:
        """Switch from silence to a real audio source (e.g., FFmpegOpusAudio)."""
        with self._lock:
            old = self._source
            self._source = source
            self._done_event = done_event
            self._loop = loop
        if old:
            old.cleanup()

    def read(self) -> bytes:
        with self._lock:
            if self._source is None:
                return self.OPUS_SILENCE
            data = self._source.read()
            if data:
                return data
            # Source exhausted — switch back to silence
            src = self._source
            self._source = None
            evt = self._done_event
            loop = self._loop
            self._done_event = None
        # Outside lock: cleanup + signal
        src.cleanup()
        if evt and loop:
            loop.call_soon_threadsafe(evt.set)
        return self.OPUS_SILENCE

    def interrupt(self) -> None:
        """Stop current TTS and switch to silence. Does NOT call vc.stop()."""
        with self._lock:
            src = self._source
            self._source = None
            evt = self._done_event
            loop = self._loop
            self._done_event = None
        if src:
            src.cleanup()
        if evt and loop:
            loop.call_soon_threadsafe(evt.set)

    def is_playing_content(self) -> bool:
        """True if playing real audio (TTS), False if playing silence."""
        return self._source is not None

    def is_opus(self) -> bool:
        return True

    def cleanup(self) -> None:
        with self._lock:
            if self._source:
                self._source.cleanup()
                self._source = None


# --- Voice: WhisperVADSink ---

# Base class is only available when voice deps are installed
_SinkBase = voice_recv.AudioSink if VOICE_AVAILABLE else object

class WhisperVADSink(_SinkBase):
    """Per-user PCM buffering with Silero VAD (ML-based voice activity detection).

    Receives 20ms PCM chunks (48kHz, stereo, 16-bit) from discord-ext-voice-recv.
    Converts to 16kHz mono for Silero, buffers speech, detects silence, dispatches.
    """

    # Discord voice: 48kHz, stereo, 16-bit signed LE → 3840 bytes per 20ms frame
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2  # 16-bit
    # Silero expects 16kHz mono, 512 samples (32ms) per chunk
    SILERO_RATE = 16000
    SILERO_CHUNK = 512  # samples per Silero frame

    _vad_model = None

    @classmethod
    def _get_vad(cls):
        if cls._vad_model is None:
            torch.set_num_threads(1)
            cls._vad_model = load_silero_vad()
            log.info("Silero VAD model loaded")
        return cls._vad_model

    def __init__(self, guild_id: int, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.guild_id = guild_id
        self.loop = loop
        self._users: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._vad = self._get_vad()
        # Accumulator for resampled audio (Silero needs 512 samples = 32ms at 16kHz)
        self._resample_buf: dict[int, bytearray] = {}
        # 2s false interruption recovery timer (LiveKit pattern)
        self._false_interrupt_timer: asyncio.TimerHandle | None = None
        # 3s force-flush timer — if Discord stops sending frames after interrupt
        self._force_flush_timer: asyncio.TimerHandle | None = None
        # Short utterance holdback — held PCM and timers per user
        self._holdback_pcm: dict[int, bytes] = {}
        self._holdback_timers: dict[int, asyncio.TimerHandle] = {}

    def wants_opus(self) -> bool:
        return False

    def _pcm_to_16k_mono(self, pcm: bytes) -> bytes:
        """Convert 48kHz stereo PCM to 16kHz mono PCM for Silero VAD."""
        mono = audioop.tomono(pcm, 2, 0.5, 0.5)
        mono_16k, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
        return mono_16k

    # Frame counter for periodic logging (avoid spamming every 20ms)
    _frame_count = 0
    _last_prob = 0.0

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None:
            return
        vc = self.voice_client
        if vc and vc.user and user.id == vc.user.id:
            return
        if not is_authorized(user.id):
            return
        if user.id in _voice_deafened:
            return

        pcm = data.pcm
        if not pcm:
            return

        with self._lock:
            uid = user.id
            if uid not in self._users:
                self._users[uid] = {
                    "buffer": bytearray(),
                    "speaking": False,
                    "timer": None,
                    "user": user,
                    "hot_frames": 0,
                    "speech_start": 0.0,   # monotonic time when speaking became True
                    "tts_interrupted": False,  # True once TTS interrupt fired this utterance
                    "lookback": deque(maxlen=10),  # rolling 200ms pre-VAD buffer
                }
                self._resample_buf[uid] = bytearray()
                log.info("Voice: new user registered in sink: %s (id=%d)", user, uid)
            state = self._users[uid]

            # Log frame arrival every 5s (~250 frames) to confirm audio is flowing
            WhisperVADSink._frame_count += 1
            if WhisperVADSink._frame_count % 250 == 1:
                buf_secs = len(state["buffer"]) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
                log.info("Voice: frame #%d from %s | speaking=%s hot=%d buf=%.1fs prob=%.2f",
                         WhisperVADSink._frame_count, user, state["speaking"],
                         state["hot_frames"], buf_secs, WhisperVADSink._last_prob)

            # Convert to 16kHz mono and accumulate for Silero's 512-sample chunks
            mono_16k = self._pcm_to_16k_mono(pcm)
            self._resample_buf[uid].extend(mono_16k)

            chunk_bytes = self.SILERO_CHUNK * 2  # 16-bit = 2 bytes/sample
            buf = self._resample_buf[uid]

            # Process all complete Silero chunks
            while len(buf) >= chunk_bytes:
                chunk_raw = bytes(buf[:chunk_bytes])
                del buf[:chunk_bytes]

                # Convert to float tensor for Silero
                audio = torch.frombuffer(chunk_raw, dtype=torch.int16).float() / 32768.0
                prob = self._vad(audio, self.SILERO_RATE).item()
                WhisperVADSink._last_prob = prob

                is_speech = prob >= VAD_SPEECH_THRESHOLD

                # Always push PCM into lookback ring (captures audio before VAD fires)
                if not state["speaking"]:
                    state["lookback"].append(bytes(pcm))

                if is_speech:
                    state["hot_frames"] += 1
                    if not state["speaking"] and state["hot_frames"] >= VAD_CONFIRM_FRAMES:
                        state["speaking"] = True
                        state["speech_start"] = time.monotonic()
                        # Prepend lookback frames to capture speech onset before VAD detected it
                        lookback_pcm = b"".join(state["lookback"])
                        state["lookback"].clear()
                        # Insert lookback BEFORE the hot frames already buffered
                        existing = bytes(state["buffer"])
                        state["buffer"].clear()
                        # Prepend any held PCM from a previous short utterance (continuation merge)
                        held_pcm = self._holdback_pcm.pop(uid, b"")
                        if held_pcm:
                            held_secs = len(held_pcm) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
                            if uid in self._holdback_timers:
                                self._holdback_timers.pop(uid).cancel()
                            log.info("Voice: merging held PCM (%.2fs) into new utterance for %s", held_secs, user)
                        state["buffer"].extend(held_pcm + lookback_pcm + existing)
                        buf_secs = len(state["buffer"]) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH) if state["buffer"] else 0
                        log.info("Voice: >>> SPEECH START — %s (p=%.2f, confirmed after %d frames, buf=%.1fs, lookback=%dms)",
                                 user, prob, state["hot_frames"], buf_secs, len(lookback_pcm) // (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH // 1000) if lookback_pcm else 0)
                    if (state["speaking"] and not state["tts_interrupted"]
                            and (time.monotonic() - state["speech_start"]) >= VAD_INTERRUPT_DELAY):
                        psrc = _persistent_source.get(self.guild_id)
                        if psrc and psrc.is_playing_content():
                            # Interrupt TTS by switching to silence — keeps
                            # outbound audio flowing so Discord doesn't kill
                            # inbound frames. No vc.stop() needed.
                            psrc.interrupt()
                            _voice_interrupted[self.guild_id] = True
                            state["tts_interrupted"] = True
                            log.info("Voice: interrupted — %s speaking %.2fs (Silero p=%.2f)",
                                     user, time.monotonic() - state["speech_start"], prob)
                            # Start 2s false interruption recovery timer
                            if self._false_interrupt_timer is not None:
                                self._false_interrupt_timer.cancel()
                            self.loop.call_soon_threadsafe(self._start_false_interrupt_timer)
                            # Start 3s force-flush timer in case Discord stops sending frames
                            if self._force_flush_timer is not None:
                                self._force_flush_timer.cancel()
                            self.loop.call_soon_threadsafe(self._start_force_flush_timer, uid)
                        elif psrc:
                            log.info("Voice: speech during silence from %s (p=%.2f) — not interrupting",
                                     user, prob)
                    # Buffer PCM from first hot frame (pre-buffer captures first syllable)
                    # and during confirmed speech. Single path avoids double-buffering.
                    state["buffer"].extend(pcm)
                    if state["speaking"] and state["timer"] is not None:
                        state["timer"].cancel()
                        state["timer"] = None
                else:
                    if state["hot_frames"] > 0 and not state["speaking"]:
                        # Pre-buffer had unconfirmed speech — clear it (was noise)
                        state["buffer"].clear()
                    state["hot_frames"] = 0
                    if state["speaking"]:
                        state["buffer"].extend(pcm)
                        if state["timer"] is None:
                            log.info("Voice: silence detected — starting %.1fs timeout (p=%.2f)",
                                     VAD_SILENCE_TIMEOUT, prob)
                            state["timer"] = self.loop.call_later(
                                VAD_SILENCE_TIMEOUT,
                                self._on_silence, uid,
                            )

    def _on_silence(self, user_id: int) -> None:
        """Called on the event loop when silence timeout fires."""
        log.info("Voice: _on_silence fired for user %d", user_id)
        # Cancel force-flush timer — _on_silence is handling it
        if self._force_flush_timer is not None:
            self._force_flush_timer.cancel()
            self._force_flush_timer = None
        with self._lock:
            state = self._users.get(user_id)
            if state is None:
                log.warning("Voice: _on_silence called for unknown user %d", user_id)
                return
            pcm_data = bytes(state["buffer"])
            user = state["user"]
            state["buffer"].clear()
            state["speaking"] = False
            state["timer"] = None
            state["hot_frames"] = 0
            state["tts_interrupted"] = False
            self._vad.reset_states()
            self._resample_buf[user_id] = bytearray()

        # Check minimum speech duration
        duration = len(pcm_data) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        log.info("Voice: <<< SPEECH END — %s (%.2fs, %d bytes) | state reset + VAD cleared",
                 user, duration, len(pcm_data))
        if duration < VAD_MIN_SPEECH_DURATION:
            log.info("Voice: ignoring short utterance (%.2fs < %.2fs min) from %s",
                     duration, VAD_MIN_SPEECH_DURATION, user)
            # Auto-resume if this was a false interruption
            vs = _get_voice_state(self.guild_id)
            if vs["interrupted"] and vs["pending_chunks"]:
                task = self.loop.create_task(_resume_interrupted_playback(self.guild_id))
                _voice_pipeline_task[self.guild_id] = task
            return

        log.info("Voice: utterance from %s (%.1fs)", user, duration)
        # Real utterance arrived — cancel false interruption timer
        if self._false_interrupt_timer is not None:
            self._false_interrupt_timer.cancel()
            self._false_interrupt_timer = None
        # Short utterance holdback — wait for continuation before dispatching
        if duration < VAD_SHORT_UTTERANCE_SECS:
            log.info("Voice: short utterance (%.2fs < %.2fs) — holdback for %.1fs, waiting for continuation",
                     duration, VAD_SHORT_UTTERANCE_SECS, VAD_HOLDBACK_WINDOW)
            if user_id in self._holdback_timers:
                self._holdback_timers.pop(user_id).cancel()
            self._holdback_pcm[user_id] = pcm_data
            self._holdback_timers[user_id] = self.loop.call_later(
                VAD_HOLDBACK_WINDOW, self._dispatch_holdback, user_id, pcm_data, user
            )
            return
        # Cancel any in-flight pipeline for this guild before starting a new one
        old_task = _voice_pipeline_task.get(self.guild_id)
        if old_task and not old_task.done():
            old_task.cancel()
            log.info("Voice: cancelled previous pipeline for new utterance")
        # _on_silence runs on the event loop (via call_later), so create_task is correct
        task = self.loop.create_task(
            process_voice_utterance(pcm_data, user, self.guild_id)
        )
        _voice_pipeline_task[self.guild_id] = task

    def _start_false_interrupt_timer(self) -> None:
        """Schedule 2s false interruption recovery (called on event loop via call_soon_threadsafe)."""
        self._false_interrupt_timer = self.loop.call_later(
            2.0, self._on_false_interrupt_timeout
        )

    def _on_false_interrupt_timeout(self) -> None:
        """2s after interruption with no real utterance — auto-resume playback."""
        self._false_interrupt_timer = None
        vs = _get_voice_state(self.guild_id)
        if not vs["interrupted"] or not vs["pending_chunks"]:
            return
        # If a real pipeline is already running, don't interfere
        task = _voice_pipeline_task.get(self.guild_id)
        if task and not task.done():
            return
        # If user is still actively speaking, don't resume — let _on_silence handle it
        with self._lock:
            anyone_speaking = any(s["speaking"] for s in self._users.values())
        if anyone_speaking:
            log.info("Voice: false interrupt timer fired but user still speaking — deferring to _on_silence")
            return
        log.info("Voice: false interruption (2s timeout) — auto-resuming playback")
        task = self.loop.create_task(_resume_interrupted_playback(self.guild_id))
        _voice_pipeline_task[self.guild_id] = task

    def _start_force_flush_timer(self, user_id: int) -> None:
        """Schedule 3s force-flush (called on event loop via call_soon_threadsafe).

        Discord sometimes stops sending audio frames after vc.stop() interrupts
        playback. If _on_silence never fires because no frames arrive, this timer
        force-flushes any buffered speech and processes it.
        """
        if self._force_flush_timer is not None:
            self._force_flush_timer.cancel()
        self._force_flush_timer = self.loop.call_later(
            3.0, self._on_force_flush, user_id
        )

    def _on_force_flush(self, user_id: int) -> None:
        """3s after interrupt: if _on_silence hasn't fired, force-flush the buffer."""
        self._force_flush_timer = None
        with self._lock:
            state = self._users.get(user_id)
            if state is None:
                return
            # If _on_silence already handled it, buffer is empty and speaking is False
            if not state["speaking"] and len(state["buffer"]) == 0:
                log.info("Voice: force-flush: _on_silence already handled it, nothing to do")
                return
            # If user is still speaking or in a mid-sentence pause, defer — don't split utterance.
            # hot_frames > 0 means active speech; timer is not None means silence timeout pending
            # (user paused but _on_silence hasn't fired yet — let it handle naturally).
            if state["speaking"] and (state["hot_frames"] > 0 or state["timer"] is not None):
                log.info("Voice: force-flush deferred — user still speaking (hot=%d, timer=%s)",
                         state["hot_frames"], "pending" if state["timer"] else "none")
                self._force_flush_timer = self.loop.call_later(
                    3.0, self._on_force_flush, user_id
                )
                return
            # Force-flush: grab buffer and reset state
            pcm_data = bytes(state["buffer"])
            user = state["user"]
            state["buffer"].clear()
            state["speaking"] = False
            state["timer"] = None
            state["hot_frames"] = 0
            state["tts_interrupted"] = False
            self._vad.reset_states()
            self._resample_buf[user_id] = bytearray()

        duration = len(pcm_data) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        log.info("Voice: force-flush fired — %s (%.2fs, %d bytes) | Discord stopped sending frames",
                 user, duration, len(pcm_data))

        if duration < VAD_MIN_SPEECH_DURATION:
            log.info("Voice: force-flush too short (%.2fs), ignoring", duration)
            return

        log.info("Voice: force-flush processing as utterance from %s (%.1fs)", user, duration)
        # Cancel false interrupt timer if still pending
        if self._false_interrupt_timer is not None:
            self._false_interrupt_timer.cancel()
            self._false_interrupt_timer = None
        old_task = _voice_pipeline_task.get(self.guild_id)
        if old_task and not old_task.done():
            old_task.cancel()
        task = self.loop.create_task(
            process_voice_utterance(pcm_data, user, self.guild_id)
        )
        _voice_pipeline_task[self.guild_id] = task

    def _dispatch_holdback(self, user_id: int, pcm_data: bytes, user) -> None:
        """Holdback timer expired — no continuation arrived, dispatch the short utterance."""
        self._holdback_pcm.pop(user_id, None)
        self._holdback_timers.pop(user_id, None)
        duration = len(pcm_data) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        log.info("Voice: holdback expired — dispatching short utterance from %s (%.2fs)", user, duration)
        old_task = _voice_pipeline_task.get(self.guild_id)
        if old_task and not old_task.done():
            old_task.cancel()
        task = self.loop.create_task(
            process_voice_utterance(pcm_data, user, self.guild_id)
        )
        _voice_pipeline_task[self.guild_id] = task

    def cleanup(self) -> None:
        if self._false_interrupt_timer is not None:
            self._false_interrupt_timer.cancel()
            self._false_interrupt_timer = None
        if self._force_flush_timer is not None:
            self._force_flush_timer.cancel()
            self._force_flush_timer = None
        for timer in self._holdback_timers.values():
            timer.cancel()
        self._holdback_timers.clear()
        self._holdback_pcm.clear()
        with self._lock:
            for state in self._users.values():
                if state["timer"] is not None:
                    state["timer"].cancel()
            self._users.clear()


# --- Voice: pipeline functions ---

def convert_pcm_to_wav(pcm_data: bytes) -> bytes:
    """Convert 48kHz stereo PCM to 16kHz mono WAV for Groq/Whisper."""
    # Stereo to mono — average channels (0.5+0.5), NOT sum (1.0+1.0) which clips
    mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
    # Downsample 48kHz → 16kHz (ratio 3:1)
    mono_16k, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(mono_16k)
    return buf.getvalue()




async def synthesize_speech(text: str) -> Path:
    """Convert text to MP3 via edge-tts. Returns path to temp MP3 file."""
    # Collapse double line breaks to sentence breaks and single newlines to spaces.
    # Edge TTS interprets \n\n as extended silence pauses.
    text = re.sub(r'\n\n+', '. ', text)
    text = text.replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    tmp = Path(tempfile.gettempdir()) / f"tts-{os.getpid()}-{id(text)}.mp3"
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE)
    await communicate.save(str(tmp))
    return tmp


def _split_into_chunks(text: str) -> list[str]:
    """Split response into speakable chunks (paragraphs)."""
    chunks = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not chunks and text.strip():
        return [text.strip()]
    return chunks


async def _build_voice_prompt(transcript: str, guild_id: int, skip_weave: bool = False) -> tuple[str, str]:
    """Build prompt + system context for voice conversation with weave support.

    Returns (prompt, extra_system).
    """
    vs = _get_voice_state(guild_id)

    # Build conversation history from voice-specific history
    lines = []
    for role, text in vs["history"]:
        lines.append(f"{role}: {text}")

    prompt_parts = []
    if lines:
        prompt_parts.append("Previous conversation:\n" + "\n".join(lines))
    prompt_parts.append(f"User: {transcript}")
    prompt = "\n\n".join(prompt_parts)

    # Build continuation context from interrupt stack.
    # Current response's interrupted state takes priority, then stack top.
    # skip_weave=True when pop_stack fired — the transcript already has resume instructions.
    extra = VOICE_SYSTEM_PROMPT
    # Inject channel history as context (replaces file-based RAM)
    channel_context = await _fetch_channel_context(guild_id)
    if channel_context:
        extra += f"\n\n[Recent conversation history from this voice session]\n{channel_context}"

    if skip_weave:
        int_spoken = []
        int_pending = []
    elif vs["interrupted"]:
        int_spoken = vs["spoken_chunks"]
        int_pending = vs["pending_chunks"]
    elif vs["interrupt_stack"]:
        top = vs["interrupt_stack"][-1]
        int_spoken = top["spoken"]
        int_pending = top["pending"]
    else:
        int_spoken = []
        int_pending = []

    has_context = bool(int_spoken or int_pending)
    stack_depth = len(vs["interrupt_stack"])

    log.info("Voice: [weave] interrupted=%s stack_depth=%d int_spoken=%d int_pending=%d has_context=%s",
             vs["interrupted"], stack_depth, len(int_spoken), len(int_pending), has_context)

    if has_context:
        spoken = "\n\n".join(int_spoken) if int_spoken else "(nothing)"
        pending = "\n\n".join(int_pending) if int_pending else ""
        extra += (
            f"\n\nCONTINUATION CONTEXT — An earlier response was interrupted.\n"
            f"What you already said (user partially heard this):\n\"{spoken}\"\n\n"
        )
        if pending:
            extra += (
                f"What remained unsaid:\n\"{pending}\"\n\n"
                f"If the user says 'carry on' / 'continue' / 'go on', resume naturally from "
                f"where you left off. Don't repeat what was already said."
            )
        else:
            extra += (
                "You were interrupted mid-sentence — the user may not have heard your full response. "
                "If the user says 'carry on' / 'continue' / 'go on', rephrase or finish "
                "what you were saying. Don't repeat verbatim."
            )
        if stack_depth > 1:
            extra += (
                f"\n\n(There are {stack_depth - 1} more interrupted topics the user may return to.)"
            )

    return prompt, extra


async def play_chunks_with_interruption(
    chunks: list[str],
    voice_client,
    guild_id: int,
    loop: asyncio.AbstractEventLoop,
) -> tuple[list[str], list[str]]:
    """Play TTS chunks via persistent source. Returns (spoken, pending) on completion or interruption.

    Pre-synthesizes the next chunk while the current one plays to minimize
    inter-chunk silence (gap becomes file handoff, not full TTS API call).
    """
    spoken = []
    psrc = _persistent_source.get(guild_id)
    if psrc is None:
        log.warning("Voice: no persistent source for guild %d, cannot play", guild_id)
        return spoken, chunks

    # Pre-synthesize first chunk before entering the loop
    next_mp3: Path | None = await synthesize_speech(chunks[0]) if chunks else None

    for i, chunk in enumerate(chunks):
        if not voice_client.is_connected():
            if next_mp3:
                next_mp3.unlink(missing_ok=True)
            return spoken, chunks[i:]

        # Clear interrupted flag before playing
        _voice_interrupted[guild_id] = False

        mp3_path = next_mp3
        next_mp3 = None

        # Start pre-synthesizing the next chunk while this one plays
        prefetch_task = None
        if i + 1 < len(chunks):
            prefetch_task = asyncio.create_task(synthesize_speech(chunks[i + 1]))

        try:
            if not voice_client.is_connected():
                if prefetch_task:
                    prefetch_task.cancel()
                return spoken, chunks[i:]
            ffmpeg_source = discord.FFmpegOpusAudio(str(mp3_path), executable=FFMPEG_PATH)
            done = asyncio.Event()
            # Feed TTS into persistent source — replaces silence with real audio.
            # When FFmpeg exhausts, source auto-reverts to silence.
            # On interrupt, source.interrupt() switches to silence immediately.
            psrc.set_source(ffmpeg_source, done, loop)
            await done.wait()
        finally:
            # Windows: ffmpeg may still hold the file after stop(). Retry deletion.
            for _ in range(5):
                try:
                    mp3_path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    await asyncio.sleep(0.2)

        # Collect pre-synthesized next chunk
        if prefetch_task:
            if _voice_interrupted.get(guild_id):
                prefetch_task.cancel()
                # Clean up any pre-synthesized file
                try:
                    pre_mp3 = await prefetch_task
                    pre_mp3.unlink(missing_ok=True)
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                try:
                    next_mp3 = await prefetch_task
                except Exception:
                    log.warning("Voice: pre-synthesis of chunk %d failed, will synthesize inline", i + 1)
                    next_mp3 = await synthesize_speech(chunks[i + 1])

        # Check if we were interrupted during this chunk
        if _voice_interrupted.get(guild_id):
            spoken.append(chunk)  # partially heard but count it
            return spoken, chunks[i + 1:]

        spoken.append(chunk)

    return spoken, []


def _find_voice_client(guild_id: int):
    """Find voice client for guild, with VoiceRecvClient fallback."""
    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    if vc is None:
        for v in bot.voice_clients:
            if v.guild and v.guild.id == guild_id:
                vc = v
                break
    return vc


def _log_post_playback_state(guild_id: int) -> None:
    """Diagnostic: log sink and DAVE state after playback ends."""
    vc = _find_voice_client(guild_id)
    sink_alive = False
    sink_listening = False
    user_states = {}
    if vc:
        sink_listening = hasattr(vc, 'is_listening') and vc.is_listening()
        sink = getattr(vc, '_listener', None)
        if sink:
            sink_alive = True
            if hasattr(sink, '_users') and hasattr(sink, '_lock'):
                with sink._lock:
                    for uid, s in sink._users.items():
                        user_states[uid] = {
                            "speaking": s["speaking"],
                            "hot_frames": s["hot_frames"],
                            "buf_bytes": len(s["buffer"]),
                            "timer_active": s["timer"] is not None,
                        }
    log.info("Voice: [post-playback] vc=%s sink_alive=%s listening=%s frame_count=%d DAVE ok=%d fail=%d users=%s",
             vc is not None, sink_alive, sink_listening, WhisperVADSink._frame_count,
             _dave_decrypt_stats["ok"], _dave_decrypt_stats["fail"],
             user_states)
    # Schedule delayed check to verify frames still flowing
    loop = asyncio.get_event_loop()
    fc_snapshot = WhisperVADSink._frame_count
    dave_snapshot = _dave_decrypt_stats["ok"]
    loop.call_later(5.0, _delayed_frame_check, guild_id, fc_snapshot, dave_snapshot)


def _delayed_frame_check(guild_id: int, old_fc: int, old_dave: int) -> None:
    """5s after playback: check if frames are still arriving."""
    new_fc = WhisperVADSink._frame_count
    new_dave = _dave_decrypt_stats["ok"]
    delta_fc = new_fc - old_fc
    delta_dave = new_dave - old_dave
    vc = _find_voice_client(guild_id)
    connected = vc.is_connected() if vc else False
    listening = hasattr(vc, 'is_listening') and vc.is_listening() if vc else False
    # Check sink user states
    user_states = {}
    if vc:
        sink = getattr(vc, '_listener', None)
        if sink and hasattr(sink, '_users') and hasattr(sink, '_lock'):
            with sink._lock:
                for uid, s in sink._users.items():
                    user_states[uid] = {"speaking": s["speaking"], "buf_bytes": len(s["buffer"])}
    log.info("Voice: [5s check] frames_delta=%d dave_delta=%d connected=%s listening=%s users=%s",
             delta_fc, delta_dave, connected, listening, user_states)
    if delta_fc == 0:
        log.warning("Voice: [5s check] NO FRAMES received in 5s — sink may be dead!")


async def _resume_interrupted_playback(guild_id: int) -> None:
    """Resume TTS playback after a false interruption (noise, not real speech)."""
    vs = _get_voice_state(guild_id)
    chunks = vs.get("pending_chunks", [])
    if not chunks:
        return

    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    voice_client = guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        return

    log.info("Voice: auto-resuming playback (%d chunks remaining)", len(chunks))
    loop = asyncio.get_event_loop()
    try:
        spoken, pending = await play_chunks_with_interruption(
            chunks, voice_client, guild_id, loop,
        )
        vs["spoken_chunks"].extend(spoken)
        vs["pending_chunks"] = pending
        vs["interrupted"] = bool(pending)
        if not pending:
            log.info("Voice: resumed playback complete — all chunks spoken")
            _log_post_playback_state(guild_id)
    except asyncio.CancelledError:
        log.info("Voice: resume cancelled (real utterance arrived)")
        psrc = _persistent_source.get(guild_id)
        if psrc:
            psrc.interrupt()
        raise


async def process_voice_utterance(pcm_data: bytes, user: discord.User, guild_id: int) -> None:
    """Full pipeline: PCM → WAV → Groq STT → Claude → chunked TTS → playback.

    Supports 'the weave': interruptible playback with continuation tracking.
    """
    global _voice_session_id
    text_channel_id = _voice_text_channels.get(guild_id)
    text_channel = bot.get_channel(text_channel_id) if text_channel_id else None

    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    voice_client = guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        return

    vs = _get_voice_state(guild_id)

    try:
        loop = asyncio.get_event_loop()
        pipeline_t0 = datetime.now()

        # dB floor gate: reject audio below minimum volume threshold
        pcm_rms = audioop.rms(pcm_data, 2)  # 2 = sample width (16-bit)
        pcm_dbfs = 20 * math.log10(pcm_rms / 32768) if pcm_rms > 0 else -100
        log.info("Voice: PCM volume: %.1f dBFS (threshold: %.1f)", pcm_dbfs, MIN_VOLUME_DBFS)
        if pcm_dbfs < MIN_VOLUME_DBFS:
            log.info("Voice: REJECTED low volume from %s (%.1f dBFS < %.1f threshold)",
                     user, pcm_dbfs, MIN_VOLUME_DBFS)
            if vs["interrupted"] and vs["pending_chunks"]:
                await _resume_interrupted_playback(guild_id)
            return

        # 1. PCM → WAV → Groq cloud STT
        log.info("Voice: [pipeline] step 1 — PCM→WAV→STT (%d bytes PCM)", len(pcm_data))
        stt_t0 = datetime.now()
        if _get_groq_client():
            wav_data = await loop.run_in_executor(None, convert_pcm_to_wav, pcm_data)
            transcript, no_speech_prob = await transcribe_audio_groq(wav_data, fmt="wav")
        else:
            log.warning("Voice: no Groq key set, cannot transcribe")
            return
        stt_elapsed = (datetime.now() - stt_t0).total_seconds()

        if not transcript or transcript.isspace():
            log.info("Voice: empty transcript from %s (STT took %.1fs), skipping", user, stt_elapsed)
            # Auto-resume if this was a false interruption
            if vs["interrupted"] and vs["pending_chunks"]:
                await _resume_interrupted_playback(guild_id)
            return

        # No-speech probability gate: reject if Whisper thinks this isn't speech
        if no_speech_prob > NO_SPEECH_PROB_THRESHOLD:
            log.info("Voice: REJECTED high no_speech_prob from %s (%.2f > %.2f): \"%s\"",
                     user, no_speech_prob, NO_SPEECH_PROB_THRESHOLD, transcript[:50])
            if vs["interrupted"] and vs["pending_chunks"]:
                await _resume_interrupted_playback(guild_id)
            return

        # Hallucination filter: reject known Whisper phantom phrases on short utterances
        utterance_duration = len(pcm_data) / (48000 * 2)  # 16-bit mono, 48kHz
        if transcript.lower().strip().rstrip('.!,') in WHISPER_HALLUCINATIONS or transcript.lower().strip() in WHISPER_HALLUCINATIONS:
            if utterance_duration < 2.0:
                log.info("Voice: REJECTED hallucination from %s (%.1fs): \"%s\"", user, utterance_duration, transcript)
                if vs["interrupted"] and vs["pending_chunks"]:
                    await _resume_interrupted_playback(guild_id)
                return

        log.info("Voice: [pipeline] STT done (%.1fs): \"%s\"", stt_elapsed, transcript[:100])

        # Post transcript to text channel
        if text_channel:
            await text_channel.send(f"**{user.display_name}:** {transcript}")

        # 2. Transcript → Claude (with weave context + persistent session)
        # "Resolve stack" — explicit command to pop interrupt stack and resume
        _t_lower = transcript.lower().replace("-", " ")
        _t_words = set(_t_lower.split())
        pop_stack = ({"pop", "stack"} <= _t_words) or ("popstack" in _t_lower) or ("resolve" in _t_words and "stack" in _t_words)
        _did_pop_stack = False
        if pop_stack and vs["interrupt_stack"]:
            # When the user just interrupted something and says "pop stack", the top
            # of the stack IS the thing they just interrupted — they want to LEAVE it.
            # Discard it and pop the NEXT entry to resume the previous topic.
            if vs["interrupted"]:
                discarded = vs["interrupt_stack"].pop()
                log.info("Voice: POP STACK — discarding just-interrupted entry (%d spoken, %d pending)",
                         len(discarded["spoken"]), len(discarded["pending"]))

            if vs["interrupt_stack"]:
                popped = vs["interrupt_stack"].pop()
                spoken_text = "\n\n".join(popped["spoken"]) if popped["spoken"] else "(nothing)"
                pending_text = "\n\n".join(popped["pending"]) if popped["pending"] else ""
                log.info("Voice: POP STACK — resuming depth %d→%d (%d spoken, %d pending)",
                         len(vs["interrupt_stack"]) + 1, len(vs["interrupt_stack"]),
                         len(popped["spoken"]), len(popped["pending"]))
                # Build explicit resume prompt — bypass normal weave context
                transcript = "The user wants you to return to an earlier interrupted topic. "
                if pending_text:
                    transcript += (
                        f"You were interrupted mid-response. What you hadn't said yet:\n"
                        f"\"{pending_text}\"\n\n"
                        f"Say ONLY the unsaid part. Do NOT repeat or summarize what was already said."
                    )
                else:
                    transcript += (
                        f"You were interrupted mid-sentence. What you had said so far:\n"
                        f"\"{spoken_text}\"\n\n"
                        f"Finish or rephrase your thought. Do NOT repeat what was already said."
                    )
            else:
                log.info("Voice: POP STACK — stack empty after discard, nothing to resume")
                transcript = "The user said 'pop stack' but there are no earlier interrupted topics to return to. Let them know the stack is clear."
            # Clear current interrupted state
            vs["interrupted"] = False
            vs["spoken_chunks"] = []
            vs["pending_chunks"] = []
            _did_pop_stack = True
        elif pop_stack:
            log.info("Voice: POP STACK — but stack is empty, treating as normal utterance")

        log.info("Voice: [pipeline] step 2 — Claude LLM")
        llm_t0 = datetime.now()
        # Don't auto-pop after explicit pop_stack — user controls the unwind
        had_weave_context = False if _did_pop_stack else (bool(vs["interrupt_stack"]) and not vs["interrupted"])
        prompt, extra_system = await _build_voice_prompt(transcript, guild_id, skip_weave=_did_pop_stack)
        response, new_sid = await run_claude(
            prompt, extra_system=extra_system,
            session_id=_voice_session_id,
        )
        llm_elapsed = (datetime.now() - llm_t0).total_seconds()
        log.info("Voice: [pipeline] LLM done (%.1fs, %d chars)", llm_elapsed, len(response))

        # Persist Claude session for future calls
        if new_sid:
            if _voice_session_id != new_sid:
                log.info("Voice: Claude session %s → %s",
                         _voice_session_id[:12] if _voice_session_id else "none",
                         new_sid[:12])
            _voice_session_id = new_sid
            _save_session_state()

        # Update voice history (kept as fallback context if session expires)
        vs["history"].append(("User", transcript))
        vs["history"].append(("Assistant", response[:500]))

        # Track turns and rotate session when it gets bloated
        vs["turn_count"] += 1
        if vs["turn_count"] >= VOICE_ROTATION_TURNS and _voice_session_id:
            log.info("Voice: turn %d reached rotation threshold (%d), rotating session",
                     vs["turn_count"], VOICE_ROTATION_TURNS)
            if text_channel:
                await text_channel.send("*Rotating voice session to keep responses fast...*")
            await _rotate_voice_session(guild_id)

        # Post full response to text channel
        if text_channel:
            for chunk in split_message(response):
                await text_channel.send(f"> {chunk}")

        # 3. Chunked TTS playback with interruption tracking
        chunks = _split_into_chunks(response)
        if not chunks:
            return

        log.info("Voice: [pipeline] step 3 — TTS + playback (%d chunks)", len(chunks))
        tts_t0 = datetime.now()

        vs["full_response"] = response
        vs["pending_chunks"] = []
        vs["spoken_chunks"] = []
        vs["interrupted"] = False

        # Snapshot DAVE stats before TTS to detect post-playback decrypt failures
        pre_tts_ok = _dave_decrypt_stats["ok"]
        pre_tts_fail = _dave_decrypt_stats["fail"]

        spoken, pending = await play_chunks_with_interruption(
            chunks, voice_client, guild_id, loop,
        )
        tts_elapsed = (datetime.now() - tts_t0).total_seconds()

        # Log DAVE stats delta — if failures spike after TTS, epoch rotation is the cause
        post_ok = _dave_decrypt_stats["ok"] - pre_tts_ok
        post_fail = _dave_decrypt_stats["fail"] - pre_tts_fail
        log.info("Voice: DAVE stats during TTS: ok=%d fail=%d (total: ok=%d fail=%d)",
                 post_ok, post_fail, _dave_decrypt_stats["ok"], _dave_decrypt_stats["fail"])

        vs["spoken_chunks"] = spoken
        vs["pending_chunks"] = pending
        # Track interruption from the actual flag, not just pending chunks.
        # Single-chunk responses have pending=[] after interrupt but were still interrupted.
        was_interrupted = _voice_interrupted.get(guild_id, False)
        vs["interrupted"] = bool(pending) or was_interrupted

        # Push interrupted context onto stack so cascading interrupts are preserved.
        # "Continue" pops the most recent; multiple "continue"s unwind the stack.
        if vs["interrupted"]:
            vs["interrupt_stack"].append({
                "spoken": list(spoken),
                "pending": list(pending),
            })
            log.info("Voice: pushed interrupt stack (depth=%d, %d spoken, %d pending)",
                     len(vs["interrupt_stack"]), len(spoken), len(pending))
        elif had_weave_context and not was_interrupted and vs["interrupt_stack"]:
            # Response with weave context played fully — pop the consumed entry
            popped = vs["interrupt_stack"].pop()
            log.info("Voice: popped interrupt stack (depth=%d, consumed %d spoken + %d pending)",
                     len(vs["interrupt_stack"]), len(popped["spoken"]), len(popped["pending"]))

        pipeline_total = (datetime.now() - pipeline_t0).total_seconds()
        log.info("Voice: [pipeline] COMPLETE — STT=%.1fs LLM=%.1fs TTS=%.1fs total=%.1fs | spoke %d/%d chunks",
                 stt_elapsed, llm_elapsed, tts_elapsed, pipeline_total, len(spoken), len(chunks))

        if was_interrupted:
            log.info("Voice: interrupted after %d/%d chunks", len(spoken), len(chunks))
        else:
            _log_post_playback_state(guild_id)

    except asyncio.CancelledError:
        log.info("Voice: pipeline cancelled (new utterance arrived)")
        psrc = _persistent_source.get(guild_id)
        if psrc:
            psrc.interrupt()
        return
    except Exception as e:
        log.error("Voice pipeline error: %s", e, exc_info=True)
        if text_channel:
            await text_channel.send(f"Voice pipeline error: {e}")


# --- Bot setup ---

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
guild_obj = discord.Object(id=GUILD_ID)


@bot.event
async def on_ready():
    global _text_chat_lock
    if _text_chat_lock is None:
        _text_chat_lock = asyncio.Lock()
    tree.copy_global_to(guild=guild_obj)
    await tree.sync(guild=guild_obj)
    log.info("Bot started as %s. Slash commands synced to guild %d.", bot.user, GUILD_ID)


# --- Slash commands ---

@tree.command(name="help", description="Show available commands", guild=guild_obj)
async def help_command(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        return
    await interaction.response.send_message(
        "**Vault bot active.**\n"
        "`/remote` — open Claude terminal + get session URL\n"
        "`/remotestop` — kill remote session\n"
        "`/capture <text>` — save to Inbox\n"
        "`/reel <url>` — transcribe IG reel\n"
        "`/voice` — join your voice channel, listen + respond\n"
        "`/voicestop` — leave voice, extract transcript to Inbox\n"
        "`/deafen` — toggle bot listening to your voice\n"
        "`/session_close` — reset conversation context, extract transcripts\n"
        "`/restart` — restart bot (sessions persist)\n"
        "`/shutdown` — stop the bot\n"
        "Or just type a message to chat with Claude."
    )


@tree.command(name="capture", description="Save text to Inbox", guild=guild_obj)
@app_commands.describe(text="The text to capture")
async def capture_command(interaction: discord.Interaction, text: str):
    if not is_authorized(interaction.user.id):
        return
    now = datetime.now()
    filename = f"Capture {now.strftime('%Y-%m-%d %H%M%S')}.md"
    filepath = INBOX_PATH / filename
    content = f"---\ncreated: {now.strftime('%Y-%m-%d')}\n---\n\n{text}\n"
    filepath.write_text(content, encoding="utf-8")
    log.info("Captured to %s", filename)
    await interaction.response.send_message(f"Captured to `{filename}`")


@tree.command(name="reel", description="Transcribe an Instagram reel", guild=guild_obj)
@app_commands.describe(url="Instagram reel URL")
async def reel_command(interaction: discord.Interaction, url: str):
    if not is_authorized(interaction.user.id):
        return
    match = INSTAGRAM_URL_RE.search(url)
    if not match:
        await interaction.response.send_message(
            "Usage: `/reel <instagram-url>`\n\nOr just paste an Instagram reel URL directly."
        )
        return
    await interaction.response.defer()
    await handle_reel(interaction, match.group(0))


@tree.command(name="voice", description="Join your voice channel — listen, transcribe, respond via TTS", guild=guild_obj)
async def voice_command(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        return

    if not VOICE_AVAILABLE:
        await interaction.response.send_message(
            "Voice dependencies not installed. Install with:\n"
            "```\npip install discord-ext-voice-recv groq davey torch silero-vad\n```\n"
            "Also requires FFmpeg on your PATH.")
        return

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.")
        return

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild

    # Already connected?
    existing_vc = guild.voice_client
    if existing_vc is None:
        for v in bot.voice_clients:
            if v.guild and v.guild.id == guild.id:
                existing_vc = v
                break
    if existing_vc and existing_vc.is_connected():
        await interaction.response.send_message(
            f"Already in a voice channel. Use `/voicestop` first."
        )
        return

    await interaction.response.defer()

    try:
        vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        loop = asyncio.get_event_loop()
        sink = WhisperVADSink(guild.id, loop)
        vc.listen(sink)

        # Start persistent audio source — plays silence forever, TTS swaps in/out.
        # Keeps outbound audio flowing so Discord never kills inbound user audio.
        psrc = PersistentAudioSource()
        _persistent_source[guild.id] = psrc
        vc.play(psrc)
        log.info("Voice: persistent audio source started (silence)")

        _voice_text_channels[guild.id] = interaction.channel_id
        _voice_locks.setdefault(guild.id, asyncio.Lock())

        log.info("Voice: joined %s in guild %s", voice_channel, guild)
        await interaction.followup.send(
            f"Joined **{voice_channel.name}**. Listening — speak and I'll respond. "
            f"Use `/voicestop` to disconnect."
        )
    except Exception as e:
        log.error("Voice: failed to connect: %s", e, exc_info=True)
        await interaction.followup.send(f"Failed to join voice channel: {e}")


@tree.command(name="voicestop", description="Leave voice channel and extract transcript to Inbox", guild=guild_obj)
async def voicestop_command(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        return

    guild = interaction.guild
    # guild.voice_client can be None with VoiceRecvClient; fall back to bot.voice_clients
    vc = guild.voice_client
    if vc is None:
        for v in bot.voice_clients:
            if v.guild and v.guild.id == guild.id:
                vc = v
                break

    if vc is None or not vc.is_connected():
        await interaction.response.send_message("Not in a voice channel.")
        return

    await interaction.response.defer()

    try:
        if vc.is_listening():
            vc.stop_listening()
    except Exception:
        pass
    if vc.is_playing():
        vc.stop()
    await vc.disconnect()

    # Clean up persistent source and any in-flight pipeline
    psrc = _persistent_source.pop(guild.id, None)
    if psrc:
        psrc.cleanup()
    old_task = _voice_pipeline_task.pop(guild.id, None)
    if old_task and not old_task.done():
        old_task.cancel()

    _voice_text_channels.pop(guild.id, None)
    _voice_locks.pop(guild.id, None)
    _voice_interrupted.pop(guild.id, None)
    # NOTE: _voice_states is NOT cleared — session persists across leave/rejoin.
    # Only /session_close resets conversation context.

    # Extract voice transcript to Inbox
    extract_msg = await _extract_session_to_inbox(_voice_session_id, "voice")

    log.info("Voice: disconnected from guild %s", guild)
    await interaction.followup.send(f"Disconnected from voice channel.\n{extract_msg}")


@tree.command(name="deafen", description="Toggle whether the bot listens to your voice", guild=guild_obj)
async def deafen_command(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        return
    uid = interaction.user.id
    if uid in _voice_deafened:
        _voice_deafened.discard(uid)
        log.info("Voice: undeafened user %s (%d)", interaction.user, uid)
        await interaction.response.send_message("Now listening to your voice again.")
    else:
        _voice_deafened.add(uid)
        log.info("Voice: deafened user %s (%d)", interaction.user, uid)
        await interaction.response.send_message("Your voice will be ignored until you run `/deafen` again.")


@tree.command(name="remote", description="Open Claude Code remote session — visible terminal + URL to Discord", guild=guild_obj)
async def remote_command(interaction: discord.Interaction):
    global _remote_proc
    if not is_authorized(interaction.user.id):
        return

    if _remote_proc is not None and _remote_proc.returncode is None:
        await interaction.response.send_message("Remote session already running. Use `/remotestop` first.")
        return

    await interaction.response.defer()

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    url_file = Path(tempfile.gettempdir()) / "claude-remote-url.txt"
    url_file.unlink(missing_ok=True)

    if os.name == "nt":
        # PowerShell wrapper: runs claude remote-control in a visible window,
        # displays output AND writes URL to temp file for the bot to read.
        ps_script = (
            f"$ErrorActionPreference = 'SilentlyContinue'\n"
            f"Set-Location '{CLAUDE_PROJECT_DIR}'\n"
            f"Remove-Item '{url_file}' -ErrorAction SilentlyContinue\n"
            f"$env:CLAUDECODE = $null\n"
            f"& claude remote-control 2>&1 | ForEach-Object {{\n"
            f"    $line = $_\n"
            f"    Write-Host $line\n"
            f"    if ($line -match 'https://claude\\.ai/code/session_') {{\n"
            f"        $line | Out-File -FilePath '{url_file}' -Encoding UTF8\n"
            f"    }}\n"
            f"}}\n"
            f"Write-Host 'Remote session ended. Press any key to close.'\n"
            f"$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')\n"
        )
        wrapper_file = Path(tempfile.gettempdir()) / "claude-remote-wrapper.ps1"
        wrapper_file.write_text(ps_script, encoding="utf-8")

        try:
            _remote_proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(wrapper_file),
                env=env,
                cwd=CLAUDE_PROJECT_DIR,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as e:
            log.error("Failed to start remote session: %s", e)
            await interaction.followup.send(f"Failed to start: {e}")
            _remote_proc = None
            return

        # Poll temp file for the URL
        url_found = None
        deadline = asyncio.get_event_loop().time() + 45
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            if url_file.exists():
                text = ANSI_RE.sub("", url_file.read_text(encoding="utf-8", errors="replace"))
                match = REMOTE_URL_RE.search(text)
                if match:
                    url_found = match.group(0)
                    break
            # Check if process died
            if _remote_proc.returncode is not None:
                break

    else:
        # Non-Windows: pipe stdout to capture URL (no visible terminal)
        try:
            _remote_proc = await asyncio.create_subprocess_exec(
                "claude", "remote-control",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=CLAUDE_PROJECT_DIR,
            )
        except Exception as e:
            log.error("Failed to start remote session: %s", e)
            await interaction.followup.send(f"Failed to start: {e}")
            _remote_proc = None
            return

        buffer = ""
        url_found = None
        try:
            deadline = asyncio.get_event_loop().time() + 45
            while asyncio.get_event_loop().time() < deadline:
                try:
                    chunk = await asyncio.wait_for(_remote_proc.stdout.read(4096), timeout=5)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                text = ANSI_RE.sub("", chunk.decode(errors="replace"))
                buffer += text
                match = REMOTE_URL_RE.search(buffer)
                if match:
                    url_found = match.group(0)
                    break
        except Exception as e:
            log.error("Error reading remote session output: %s", e)

    if url_found:
        await interaction.followup.send(f"Remote session ready!\n{url_found}")
        log.info("Remote session started: %s", url_found)
    elif _remote_proc is not None and _remote_proc.returncode is not None:
        await interaction.followup.send("Remote session exited unexpectedly. Check the terminal window for errors.")
        log.error("Remote session process exited early")
        _remote_proc = None
    else:
        await interaction.followup.send("Timed out waiting for session URL (45s). The terminal may still be starting — check it.")
        log.warning("Could not find remote session URL within 45s")


@tree.command(name="remotestop", description="Stop the active remote session", guild=guild_obj)
async def remotestop_command(interaction: discord.Interaction):
    global _remote_proc
    if not is_authorized(interaction.user.id):
        return

    if _remote_proc is None or _remote_proc.returncode is not None:
        await interaction.response.send_message("No active remote session.")
        _remote_proc = None
        return

    _remote_proc.kill()
    await _remote_proc.wait()
    _remote_proc = None
    log.info("Remote session stopped by user.")
    await interaction.response.send_message("Remote session stopped.")


@tree.command(name="session_close", description="Reset conversation context, extract transcripts to Inbox", guild=guild_obj)
async def session_close_command(interaction: discord.Interaction):
    global _text_session_id, _voice_session_id
    if not is_authorized(interaction.user.id):
        return

    if not _text_session_id and not _voice_session_id:
        await interaction.response.send_message("No active sessions to close.")
        return

    await interaction.response.defer()

    results = []

    # Extract text session
    if _text_session_id:
        msg = await _extract_session_to_inbox(_text_session_id, "text")
        results.append(msg)
        log.info("session_close: cleared text session %s", _text_session_id[:12])
        _text_session_id = None

    # Extract voice session
    if _voice_session_id:
        msg = await _extract_session_to_inbox(_voice_session_id, "voice")
        results.append(msg)
        log.info("session_close: cleared voice session %s", _voice_session_id[:12])
        _voice_session_id = None

    # Clear in-memory conversation histories
    chat_history.clear()
    for vs in _voice_states.values():
        vs["history"].clear()
        vs["interrupt_stack"].clear()
        vs["pending_chunks"].clear()
        vs["spoken_chunks"].clear()
        vs["full_response"] = ""
        vs["interrupted"] = False
        vs["turn_count"] = 0

    # No file-based RAM to clear — Discord channel history is the memory

    _save_session_state()

    await interaction.followup.send(
        "**Session reset.** Fresh conversation context.\n" + "\n".join(results)
    )



@tree.command(name="shutdown", description="Shut down the bot", guild=guild_obj)
async def shutdown_command(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        return
    log.info("Shutdown requested by user %s", interaction.user.id)
    await interaction.response.send_message("Shutting down.")
    _save_session_state()
    cleanup_pid()
    await bot.close()


@tree.command(name="restart", description="Restart the bot (new PID)", guild=guild_obj)
async def restart_command(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        return
    log.info("Restart requested by user %s", interaction.user.id)
    _save_session_state()
    await interaction.response.send_message("Restarting...")

    async def _do_restart():
        await asyncio.sleep(1)
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable, str(Path(__file__))],
            creationflags=creationflags,
            close_fds=True,
        )
        cleanup_pid()
        await bot.close()

    asyncio.ensure_future(_do_restart())


# --- Free-text message handler ---

@bot.event
async def on_message(message: discord.Message):
    global _text_session_id
    # Ignore own messages
    if message.author == bot.user:
        return
    # Ignore non-authorized users
    if not is_authorized(message.author.id):
        return
    # Ignore messages that look like slash commands (interaction-based, won't appear here anyway)
    if message.content.startswith("/"):
        return

    # Dedup guard — Discord can send duplicate events with different IDs after reconnect
    dedup_key = f"{message.channel.id}:{message.author.id}:{message.content}"
    if dedup_key in _seen_message_ids:
        log.info("Dedup: dropped duplicate message from %s", message.author)
        return
    _seen_message_ids.append(dedup_key)

    user_text = message.content
    channel_id = message.channel.id

    # Auto-detect Instagram reel URLs
    ig_match = INSTAGRAM_URL_RE.search(user_text)
    if ig_match:
        await handle_reel_from_message(message, ig_match.group(0))
        return

    # With --resume, Claude has full conversation history.
    # Only use build_prompt (history stuffing) as fallback when no session exists.
    if _text_session_id:
        prompt = user_text
    else:
        prompt = build_prompt(user_text, channel_id)

    # Serialize text chat calls to prevent session ID race conditions
    async with _text_chat_lock:
        async with message.channel.typing():
            response, new_sid = await run_claude(prompt, session_id=_text_session_id)

        # Persist text session ID
        if new_sid:
            if _text_session_id != new_sid:
                log.info("Text: Claude session %s → %s",
                         _text_session_id[:12] if _text_session_id else "none",
                         new_sid[:12])
            _text_session_id = new_sid
            _save_session_state()

    # Store exchange in history (fallback if session expires)
    chat_history[channel_id].append(("User", user_text))
    chat_history[channel_id].append(("Assistant", response[:500]))

    for chunk in split_message(response):
        await message.reply(chunk, mention_author=False)


# --- Entry point ---

if __name__ == "__main__":
    kill_existing_instance()
    write_pid()
    _load_session_state()
    log.info("Bot starting (PID %d)...", os.getpid())
    try:
        bot.run(BOT_TOKEN, log_handler=None)
    finally:
        _save_session_state()
        cleanup_pid()
