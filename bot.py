"""
Gofile Waifu Bot — Premium Telegram file-uploader with cute anime girl images.

Features:
  - Upload ANY file to gofile.io → instant shareable download link
  - TWO upload paths:
    • Direct (≤20 MB): send file to bot → instant gofile link
    • Mini App (ANY size, even 15 GB+): tap "Upload Big File" button →
      opens browser → uploads directly to gofile.io (bypasses Telegram limits)
  - Fast streaming upload (no whole-file RAM load)
  - Multi-region gofile endpoints for parallel throughput
  - Premium HTML message UI with inline keyboards
  - Every bot reply attaches a different cute anime girl image (SFW)
  - 30-image pre-downloaded pool + live API fallback for variety
  - Per-user concurrency control + global semaphore
  - Built-in HTTPS-ready Mini App web server (serves /miniapp)
  - Graceful error handling with helpful messages

Usage:
  1. cp .env.example .env  → set BOT_TOKEN + MINIAPP_URL
  2. pip install -r requirements.txt
  3. python bot.py
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import shutil
import time
import traceback
from pathlib import Path
from typing import Optional

import aiohttp
import aiofiles
from aiohttp import web as aiohttp_web
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    AIORateLimiter,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

# Telethon bridge — for downloading files > 20 MB (up to 2 GB / 4 GB)
try:
    import telethon_bridge
    TELETHON_AVAILABLE_IMPORT = True
except ImportError:
    TELETHON_AVAILABLE_IMPORT = False
    telethon_bridge = None

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
WAIFU_DIR = BASE_DIR / "assets" / "waifus"

# Railway-friendly data directory:
#   - DATA_DIR env var (explicit override, e.g. Railway volume mount /data)
#   - /data if it already exists (auto-detected Railway volume mount)
#   - BASE_DIR otherwise (local dev)
DATA_DIR = Path(
    os.environ.get("DATA_DIR", "").strip()
    or ("/data" if Path("/data").is_dir() else str(BASE_DIR))
)
DOWNLOAD_DIR = DATA_DIR / "downloads"
LOG_DIR = DATA_DIR / "logs"
for _d in (DOWNLOAD_DIR, LOG_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        # Fallback to /tmp if /data isn't writable (e.g. Railway without a volume)
        fallback = Path("/tmp") / _d.name
        fallback.mkdir(parents=True, exist_ok=True)
        if _d is DOWNLOAD_DIR:
            DOWNLOAD_DIR = fallback
        else:
            LOG_DIR = fallback

load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DEV_CHAT_ID = os.environ.get("DEV_CHAT_ID", "").strip()  # for error alerts

# Optional: gofile account token (for permanent storage). Empty = guest uploads.
GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "").strip()

# Mini App configuration
# MINIAPP_URL is the PUBLIC HTTPS URL where the Mini App is reachable from the internet.
# Priority:
#   1. Explicit MINIAPP_URL env var (Cloudflare Tunnel / ngrok / custom domain)
#   2. RAILWAY_PUBLIC_DOMAIN env var (auto-provided by Railway on first deploy)
#   3. Empty (Mini App disabled — small files still work via Bot API/Telethon)
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").strip()
if not MINIAPP_URL:
    _railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if _railway_domain:
        if not _railway_domain.startswith("http"):
            _railway_domain = f"https://{_railway_domain}"
        MINIAPP_URL = f"{_railway_domain.rstrip('/')}/miniapp"

# Local HTTP server port (serves the Mini App).
# Railway auto-provides PORT env var — prefer that for production.
WEB_PORT = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or "8080")

# Path to the Mini App HTML file
MINIAPP_FILE = BASE_DIR / "miniapp" / "index.html"

# Telethon bridge performance tuning
TELETHON_PARALLEL_CHUNKS = int(os.environ.get("TELETHON_PARALLEL_CHUNKS", "3"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Always log to stdout (Railway / Docker / systemd all capture this).
# Optionally log to file — gracefully skip if the dir isn't writable
# (e.g. Railway's ephemeral filesystem without a mounted volume).
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_handlers.append(
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
    )
except (OSError, PermissionError):
    pass  # Filesystem not writable — stdout is enough on Railway
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=_log_handlers,
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
log = logging.getLogger("waifu-bot")
if MINIAPP_URL and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
    log.info("Auto-detected MINIAPP_URL from RAILWAY_PUBLIC_DOMAIN: %s", MINIAPP_URL)

# ---------------------------------------------------------------------------
# Concurrency control
# ---------------------------------------------------------------------------
GLOBAL_UPLOAD_SEM = asyncio.Semaphore(8)          # max 8 concurrent uploads bot-wide
PER_USER_SEM: dict[int, asyncio.Semaphore] = {}   # max 2 concurrent per user
PER_USER_SEM_LOCK = asyncio.Lock()


async def get_user_sem(user_id: int) -> asyncio.Semaphore:
    async with PER_USER_SEM_LOCK:
        if user_id not in PER_USER_SEM:
            PER_USER_SEM[user_id] = asyncio.Semaphore(2)
        return PER_USER_SEM[user_id]


# ---------------------------------------------------------------------------
# Waifu image pool
# ---------------------------------------------------------------------------
WAIFU_POOL: list[Path] = []
WAIFU_INDEX = 0  # round-robin pointer for guaranteed variety


def load_waifu_pool() -> None:
    global WAIFU_POOL
    WAIFU_POOL = sorted(WAIFU_DIR.glob("waifu_*.*"))
    if WAIFU_POOL:
        log.info("Loaded %d waifu images from %s", len(WAIFU_POOL), WAIFU_DIR)
    else:
        log.warning("No waifu images found in %s — bot will fetch live each time", WAIFU_DIR)


async def get_next_waifu_path() -> Optional[Path]:
    """Return next waifu image (round-robin). Always different per call."""
    global WAIFU_INDEX
    if not WAIFU_POOL:
        load_waifu_pool()
    if not WAIFU_POOL:
        return None
    path = WAIFU_POOL[WAIFU_INDEX % len(WAIFU_POOL)]
    WAIFU_INDEX += 1
    return path


# Live fallback if local pool is missing
LIVE_API_CATEGORIES = ["neko", "waifu", "kitsune"]


async def fetch_live_waifu_url(session: aiohttp.ClientSession) -> Optional[str]:
    """Try nekos.best → nekos.life. Returns a fresh image URL or None."""
    cat = random.choice(LIVE_API_CATEGORIES)
    try:
        async with session.get(
            f"https://nekos.best/api/v2/{cat}",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json()
                return data["results"][0]["url"]
    except Exception as e:
        log.debug("nekos.best/%s failed: %s", cat, e)
    try:
        async with session.get(
            f"https://nekos.life/api/v2/img/{cat if cat != 'kitsune' else 'fox_girl'}",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json()
                return data["url"]
    except Exception as e:
        log.debug("nekos.life failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Gofile upload
# ---------------------------------------------------------------------------
GOFILE_UPLOAD_URLS = [
    "https://upload.gofile.io/uploadfile",          # global auto-select
    "https://upload-eu-par.gofile.io/uploadfile",   # Europe
    "https://upload-na-phx.gofile.io/uploadfile",   # North America
    "https://upload-ap-sgp.gofile.io/uploadfile",   # Asia Pacific (Singapore)
]


async def _file_chunk_generator(
    file_path: Path,
    progress_cb,
    total: int,
    chunk_size: int = 1024 * 1024,
):
    """Async generator that streams file chunks via aiofiles + reports progress."""
    sent = 0
    last_cb = 0.0
    async with aiofiles.open(file_path, "rb") as f:
        while True:
            chunk = await f.read(chunk_size)
            if not chunk:
                break
            sent += len(chunk)
            now = time.monotonic()
            if progress_cb and now - last_cb >= 0.5:
                last_cb = now
                try:
                    await progress_cb(sent, total)
                except Exception:
                    pass
            yield chunk


async def upload_to_gofile(
    session: aiohttp.ClientSession,
    file_path: Path,
    progress_cb=None,
    folder_id: Optional[str] = None,
) -> dict:
    """
    Stream-upload a file to gofile.io. Returns parsed response `data` dict.

    - Streams the file in 1MB chunks via aiofiles (low RAM, true async)
    - Tracks byte-level upload progress
    - Retries up to 3 times across regional endpoints on failure
    """
    total = file_path.stat().st_size
    endpoint = random.choice(GOFILE_UPLOAD_URLS)

    for attempt in range(3):
        try:
            # Build a fresh async generator for each attempt
            chunk_gen = _file_chunk_generator(file_path, progress_cb, total)

            form = aiohttp.FormData()
            form.add_field(
                "file",
                chunk_gen,
                filename=file_path.name,
                content_type="application/octet-stream",
            )
            if GOFILE_TOKEN:
                form.add_field("token", GOFILE_TOKEN)
            if folder_id:
                form.add_field("folderId", folder_id)

            headers = {}
            if GOFILE_TOKEN:
                headers["Authorization"] = f"Bearer {GOFILE_TOKEN}"

            async with session.post(
                endpoint,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300),
            ) as resp:
                text = await resp.text()
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Gofile returned non-JSON: {text[:300]}")
                if body.get("status") != "ok":
                    raise RuntimeError(f"Gofile error: {body}")
                return body["data"]
        except Exception as e:
            log.warning("Gofile upload attempt %d failed: %s", attempt + 1, e)
            if attempt < 2:
                endpoint = random.choice(GOFILE_UPLOAD_URLS)
                await asyncio.sleep(1.5 * (attempt + 1))
            else:
                raise


# ---------------------------------------------------------------------------
# Helpers — file extraction from Telegram update
# ---------------------------------------------------------------------------
def extract_file_ref(update: Update):
    """Return (tg_file_obj, suggested_filename, mime_type) from any media msg."""
    msg = update.effective_message
    if msg.document:
        return msg.document, msg.document.file_name or "file.bin", msg.document.mime_type or "application/octet-stream"
    if msg.photo:
        # largest photo
        ph = msg.photo[-1]
        return ph, f"photo_{ph.file_unique_id}.jpg", "image/jpeg"
    if msg.video:
        return msg.video, msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4", msg.video.mime_type or "video/mp4"
    if msg.audio:
        return msg.audio, msg.audio.file_name or f"audio_{msg.audio.file_unique_id}.mp3", msg.audio.mime_type or "audio/mpeg"
    if msg.voice:
        return msg.voice, f"voice_{msg.voice.file_unique_id}.ogg", "audio/ogg"
    if msg.animation:
        return msg.animation, msg.animation.file_name or f"animation_{msg.animation.file_unique_id}.mp4", msg.animation.mime_type or "video/mp4"
    if msg.video_note:
        return msg.video_note, f"video_note_{msg.video_note.file_unique_id}.mp4", "video/mp4"
    return None, None, None


def human_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} PB"


def progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "░" * width
    pct = min(done * 100 // total, 100)
    filled = pct * width // 100
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Premium UI helpers
# ---------------------------------------------------------------------------
def make_caption(title: str, body: str, footer: str = "🌸 <i>Powered by Gofile Waifu Bot</i>") -> str:
    """Build a premium-styled HTML caption (≤1024 chars)."""
    sep = "━" * 22
    text = f"{title}\n{sep}\n{body}"
    if footer:
        text += f"\n{sep}\n{footer}"
    # Telegram caption hard limit
    if len(text) > 1024:
        text = text[:1020] + "…"
    return text


def result_keyboard(download_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐  Open in Browser", url=download_url)],
            [
                InlineKeyboardButton("📋  Copy Link", copy_text=download_url),
                InlineKeyboardButton("📤  Upload Another", callback_data="again"),
            ],
            [InlineKeyboardButton("❌  Close", callback_data="dismiss")],
        ]
    )


# ---------------------------------------------------------------------------
# Bot: send photo + caption (with local-pool → live-URL fallback)
# ---------------------------------------------------------------------------
async def bot_send_photo(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    reply_to_message_id: Optional[int] = None,
) -> None:
    """Send a waifu image + caption. Try local pool first, then live URL."""
    # Try local pool
    img_path = await get_next_waifu_path()
    if img_path and img_path.exists():
        try:
            with img_path.open("rb") as fh:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=fh,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                )
            return
        except Exception as e:
            log.warning("Local waifu send failed (%s): %s", img_path.name, e)

    # Fallback: live URL
    session: aiohttp.ClientSession = context.bot_data.get("http")
    if session is None:
        session = aiohttp.ClientSession()
        context.bot_data["http"] = session
    try:
        live_url = await fetch_live_waifu_url(session)
        if live_url:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=live_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
            return
    except Exception as e:
        log.warning("Live waifu URL send failed: %s", e)

    # Last resort: text-only
    await context.bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
WELCOME_TEXT = (
    "🌸 <b>Konnichiwa! Welcome to Gofile Waifu Bot</b> 🌸\n"
    "\n"
    "I upload your files to <b>gofile.io</b> and give you a fast shareable link ⚡\n"
    "\n"
    "📤 <b>Two ways to upload:</b>\n"
    "  • <b>Small files (≤20 MB):</b> just send them here directly\n"
    "  • <b>BIG files (any size — even 15 GB+):</b> tap the <b>📦 Upload Big File</b> button below!\n"
    "\n"
    "📦 <b>What you can upload:</b>\n"
    "  • Documents (PDF, ZIP, APK, EXE, anything…)\n"
    "  • Photos, Videos, Audio, Voice, GIFs\n"
    "  • Movies, ISOs, game installs — anything!\n"
    "\n"
    "⚡ <b>Speed:</b> streamed multi-region upload\n"
    "🔒 <b>Privacy:</b> guest uploads (no signup)\n"
    "🎀 <b>Vibe:</b> cute anime girl in every reply\n"
    "\n"
    "<i>Send a small file now, or tap the big-file button for huge uploads — kami-sama is watching! 🌸</i>"
)


def miniapp_button_row() -> list[InlineKeyboardButton]:
    """Return the 'Upload Big File' button if Mini App is configured, else empty."""
    if MINIAPP_URL:
        return [InlineKeyboardButton("📦  Upload Big File (any size)", web_app=WebAppInfo(url=MINIAPP_URL))]
    return []


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = []
    big_btn_row = miniapp_button_row()
    if big_btn_row:
        keyboard.append(big_btn_row)
    keyboard.append([
        InlineKeyboardButton("📚  Help", callback_data="help"),
        InlineKeyboardButton("📊  Stats", callback_data="stats"),
    ])
    await bot_send_photo(
        context,
        chat_id=update.effective_chat.id,
        caption=WELCOME_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    big_section = ""
    if MINIAPP_URL:
        big_section = (
            "\n"
            "📦 <b>Big files (any size — 15 GB+ works!):</b>\n"
            "  Tap the <b>Upload Big File</b> button → opens a browser page →\n"
            "  pick your file → it uploads directly to gofile.io from your browser\n"
            "  (bypasses Telegram's size limit entirely) → link appears here.\n"
        )
    body = (
        "📤 <b>Small files (≤20 MB):</b>\n"
        "  Just send any file directly to the chat — I'll upload it instantly.\n"
        f"{big_section}"
        "\n"
        "💡 <b>Tips:</b>\n"
        "  • Multiple small files = same shared folder link\n"
        "  • Big-file uploads never expire from gofile's free tier (10-day default)\n"
        "  • Each upload reply includes Open / Copy / Upload Another buttons\n"
        "\n"
        "⚙️ <b>Commands:</b>\n"
        "  /start — Welcome message\n"
        "  /help  — This help\n"
        "  /stats — Upload statistics\n"
        "  /cancel — Cancel an in-progress upload\n"
        "  /bigfile — Open the big-file uploader directly\n"
    )
    keyboard = []
    big_btn_row = miniapp_button_row()
    if big_btn_row:
        keyboard.append(big_btn_row)
    await bot_send_photo(
        context,
        chat_id=update.effective_chat.id,
        caption=make_caption("📚  <b>Help</b>", body),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def cmd_bigfile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the Mini App for big-file uploads."""
    if not MINIAPP_URL:
        body = (
            "❌ <b>Big-file uploader not configured.</b>\n"
            "\n"
            "The bot owner needs to set <code>MINIAPP_URL</code> in <code>.env</code>\n"
            "and expose the bot's web server via HTTPS (Cloudflare Tunnel / ngrok).\n"
            "\n"
            "See the README for setup instructions."
        )
        await bot_send_photo(
            context,
            chat_id=update.effective_chat.id,
            caption=make_caption("📦  <b>Big File Upload</b>", body),
        )
        return

    body = (
        "📦 <b>Big File Uploader</b>\n"
        "\n"
        "Tap the button below to open the uploader in your browser.\n"
        "You can upload <b>any size file</b> — even 15 GB+!\n"
        "\n"
        "<i>The file uploads directly from your browser to gofile.io,</i>\n"
        "<i>so Telegram's 20 MB / 2 GB limits don't apply.</i>\n"
        "\n"
        "🌸 <i>Once done, the link appears here automatically.</i>"
    )
    await bot_send_photo(
        context,
        chat_id=update.effective_chat.id,
        caption=make_caption("📦  <b>Big File Uploader</b>", body),
        reply_markup=InlineKeyboardMarkup([miniapp_button_row()]),
    )


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive data from the Mini App (the gofile URL after big-file upload)."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    data_str = msg.web_app_data.data if msg and msg.web_app_data else ""

    log.info("WebApp data from %s: %s", user.id, data_str[:200])

    try:
        payload = json.loads(data_str)
        url = payload.get("url", "")
        name = payload.get("name", "file")
        size = int(payload.get("size", 0))
    except (json.JSONDecodeError, TypeError):
        # Maybe it's a plain URL string
        url = data_str.strip()
        name = "file"
        size = 0

    if not url or not url.startswith("http"):
        body = (
            "❌ <b>Couldn't read the upload result from the Mini App.</b>\n"
            "Please try again, or paste your gofile link manually."
        )
        await bot_send_photo(
            context,
            chat_id=chat.id,
            caption=make_caption("❌  <b>Error</b>", body),
        )
        return

    safe_name = html.escape(name)
    size_str = human_size(size) if size else "—"
    # Extract share code from URL for display
    share_code = url.rstrip("/").split("/")[-1] if "/" in url else url

    body = (
        f"📁 <b>File:</b> <code>{safe_name}</code>\n"
        f"💾 <b>Size:</b> <code>{size_str}</code>\n"
        f"🎭 <b>Source:</b> <i>Browser upload (no size limit)</i>\n"
        f"🔗 <b>Link:</b> <a href=\"{url}\">{html.escape(share_code)}</a>\n"
    )

    # Update stats
    stats = context.bot_data.setdefault("stats", {"uploads": 0, "bytes": 0, "fails": 0})
    stats["uploads"] += 1
    stats["bytes"] += size

    await bot_send_photo(
        context,
        chat_id=chat.id,
        caption=make_caption("✨  <b>Big File Upload Complete!</b>", body),
        reply_markup=result_keyboard(url),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = context.bot_data.get("stats", {"uploads": 0, "bytes": 0, "fails": 0})
    body = (
        f"✅ <b>Successful uploads:</b> <code>{stats['uploads']}</code>\n"
        f"💾 <b>Total bytes uploaded:</b> <code>{human_size(stats['bytes'])}</code>\n"
        f"❌ <b>Failures:</b> <code>{stats['fails']}</code>\n"
        f"🖼 <b>Waifu pool size:</b> <code>{len(WAIFU_POOL)}</code> images\n"
    )
    await bot_send_photo(
        context,
        chat_id=update.effective_chat.id,
        caption=make_caption("📊  <b>Bot Statistics</b>", body),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    body = (
        "👌 <i>No active upload task was found for you.</i>\n"
        "If your upload is stuck, just send the file again — I'll handle it fresh!"
    )
    await bot_send_photo(
        context,
        chat_id=update.effective_chat.id,
        caption=make_caption("🛑  <b>Cancel</b>", body),
    )


# Track per-user "shared folder" so multi-file batches share one link
async def get_user_folder(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[Optional[str], Optional[str]]:
    """Return (folder_id, guest_token) if we have one cached for this user."""
    ud = context.user_data
    # folder cache expires after 30 minutes of inactivity
    if ud.get("folder_ts") and time.time() - ud["folder_ts"] < 1800:
        return ud.get("folder_id"), ud.get("guest_token")
    return None, None


async def save_user_folder(context: ContextTypes.DEFAULT_TYPE, user_id: int, folder_id: str, guest_token: str) -> None:
    ud = context.user_data
    ud["folder_id"] = folder_id
    ud["guest_token"] = guest_token
    ud["folder_ts"] = time.time()


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main handler: receive file → upload to gofile → reply with link + waifu."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    tg_file, name, mime = extract_file_ref(update)
    if tg_file is None:
        return

    # Sanitize filename
    safe_name = html.escape(name or "file.bin")
    size_bytes = tg_file.file_size or 0
    size_str = human_size(size_bytes)

    # Determine download path based on size:
    #   - <= 20 MB: PTB cloud Bot API
    #   - 20 MB - 2 GB (or 4 GB if Premium sender): Telethon MTProto bridge
    #   - > 2 GB: suggest the Mini App (browser-direct upload)
    use_telethon = size_bytes > 20 * 1024 * 1024 and size_bytes <= 4 * 1024 * 1024 * 1024
    telethon_ready = context.bot_data.get("telethon_ready", False)

    if use_telethon and not telethon_ready:
        # Telethon not configured — suggest Mini App if available, else error
        if MINIAPP_URL:
            body = (
                f"📁 <b>File:</b> <code>{safe_name}</code>\n"
                f"💾 <b>Size:</b> <code>{size_str}</code>\n"
                "\n"
                "⚠️ <b>This file is larger than 20 MB.</b>\n"
                "Direct download isn't configured (API_ID/API_HASH missing in .env).\n"
                "\n"
                "<i>Use the button below to upload via the browser Mini App —</i>\n"
                "<i>it handles any file size, including 15 GB+.</i>"
            )
            await bot_send_photo(
                context,
                chat_id=chat.id,
                caption=make_caption("📦  <b>Use the Big-File Uploader</b>", body),
                reply_markup=InlineKeyboardMarkup([miniapp_button_row()]),
                reply_to_message_id=msg.message_id,
            )
        else:
            body = (
                f"📁 <b>File:</b> <code>{safe_name}</code>\n"
                f"💾 <b>Size:</b> <code>{size_str}</code>\n"
                "\n"
                "⚠️ <b>This file is larger than 20 MB.</b>\n"
                "To enable direct uploads up to 2 GB (4 GB from Premium senders):\n"
                "  1. Get <code>API_ID</code> + <code>API_HASH</code> from https://my.telegram.org/apps\n"
                "  2. Add them to <code>.env</code> and restart the bot\n"
                "\n"
                "<i>Or use the big-file Mini App for files of any size.</i>"
            )
            await bot_send_photo(
                context,
                chat_id=chat.id,
                caption=make_caption("❌  <b>Large file needs setup</b>", body),
                reply_to_message_id=msg.message_id,
            )
        return

    if size_bytes > 4 * 1024 * 1024 * 1024:
        # File bigger than 4 GB — Telegram can't store it unless sender is Premium
        # Even then, 4 GB is the absolute ceiling. Suggest Mini App.
        body = (
            f"📁 <b>File:</b> <code>{safe_name}</code>\n"
            f"💾 <b>Size:</b> <code>{size_str}</code>\n"
            "\n"
            "⚠️ <b>This file is larger than 4 GB.</b>\n"
            "Telegram's hard limit is 4 GB (Premium senders only) — this file can't be\n"
            "downloaded from Telegram even with the best setup.\n"
            "\n"
            "<i>Tap the button below to upload via the browser Mini App —</i>\n"
            "<i>it bypasses Telegram entirely and supports any size (15 GB+).</i>"
        )
        await bot_send_photo(
            context,
            chat_id=chat.id,
            caption=make_caption("📦  <b>Use the Big-File Uploader</b>", body),
            reply_markup=InlineKeyboardMarkup([miniapp_button_row()]) if MINIAPP_URL else None,
            reply_to_message_id=msg.message_id,
        )
        return

    # Acquire per-user + global semaphores
    user_sem = await get_user_sem(user.id)
    status_msg = None
    dest_path: Optional[Path] = None

    try:
        async with user_sem:
            async with GLOBAL_UPLOAD_SEM:
                # Send initial "Downloading" status
                download_method = "Telethon (MTProto)" if use_telethon else "Bot API"
                status_msg = await msg.reply_text(
                    f"⬇️ <b>Downloading</b> <code>{safe_name}</code> ({size_str})\n"
                    f"<i>via {download_method}</i>…",
                    parse_mode=ParseMode.HTML,
                )
                await context.bot.send_chat_action(chat.id, ChatAction.TYPING)

                # 1) Download from Telegram
                dest_path = DOWNLOAD_DIR / f"{user.id}_{int(time.time())}_{name}"

                download_progress_state = {"last_edit": 0.0}

                async def download_progress_cb(done: int, total: int) -> None:
                    """Progress callback for Telethon download."""
                    now = time.monotonic()
                    if now - download_progress_state["last_edit"] < 1.0:
                        return
                    download_progress_state["last_edit"] = now
                    pct = done * 100 // total if total else 0
                    bar = progress_bar(done, total)
                    try:
                        await status_msg.edit_text(
                            f"⬇️ <b>Downloading</b> <code>{safe_name}</code>\n"
                            f"<code>{bar}</code> {pct}%  ({human_size(done)}/{human_size(total)})\n"
                            f"<i>via Telethon MTProto</i>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

                try:
                    if use_telethon:
                        # Use Telethon for files > 20 MB
                        log.info("Using Telethon bridge for %s (%d bytes)", name, size_bytes)
                        await telethon_bridge.download_large_file(
                            chat_id=chat.id,
                            message_id=msg.message_id,
                            dest_path=dest_path,
                            progress_cb=download_progress_cb,
                            parallel=TELETHON_PARALLEL_CHUNKS,
                        )
                    else:
                        # Use PTB cloud Bot API for small files
                        tg_file_obj = await context.bot.get_file(tg_file.file_id)
                        await tg_file_obj.download_to_drive(
                            dest_path,
                            read_timeout=120,
                            write_timeout=120,
                        )
                except Exception as e:
                    log.error("Telegram download failed: %s", e)
                    body = (
                        f"📁 <b>File:</b> <code>{safe_name}</code>\n"
                        f"💾 <b>Size:</b> <code>{size_str}</code>\n"
                        f"\n❌ <b>Download from Telegram failed:</b>\n<code>{html.escape(repr(e))[:300]}</code>"
                    )
                    await status_msg.delete()
                    await bot_send_photo(
                        context,
                        chat_id=chat.id,
                        caption=make_caption("❌  <b>Download Failed</b>", body),
                        reply_to_message_id=msg.message_id,
                    )
                    return

                # Update status: uploading
                await status_msg.edit_text(
                    f"⬆️ <b>Uploading</b> <code>{safe_name}</code> to gofile.io…",
                    parse_mode=ParseMode.HTML,
                )
                await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_DOCUMENT)

                # 2) Upload to gofile with progress updates
                last_edit = [0.0]

                async def progress_cb(sent: int, total: int) -> None:
                    now = time.monotonic()
                    if now - last_edit[0] < 1.0:
                        return
                    last_edit[0] = now
                    pct = sent * 100 // total if total else 0
                    bar = progress_bar(sent, total)
                    try:
                        await status_msg.edit_text(
                            f"⬆️ <b>Uploading</b> <code>{safe_name}</code>\n"
                            f"<code>{bar}</code> {pct}%  ({human_size(sent)}/{human_size(total)})",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass  # tolerate edit-rate-limit

                session: aiohttp.ClientSession = context.bot_data["http"]
                folder_id, guest_token = await get_user_folder(context, user.id)

                try:
                    result = await upload_to_gofile(
                        session,
                        dest_path,
                        progress_cb=progress_cb,
                        folder_id=folder_id,
                    )
                except Exception as e:
                    log.error("Gofile upload failed: %s", e)
                    stats = context.bot_data.setdefault("stats", {"uploads": 0, "bytes": 0, "fails": 0})
                    stats["fails"] += 1
                    body = (
                        f"📁 <b>File:</b> <code>{safe_name}</code>\n"
                        f"💾 <b>Size:</b> <code>{size_str}</code>\n"
                        f"\n❌ <b>Gofile upload failed:</b>\n<code>{html.escape(repr(e))[:300]}</code>\n"
                        "\n<i>Please try again in a moment.</i>"
                    )
                    await status_msg.delete()
                    await bot_send_photo(
                        context,
                        chat_id=chat.id,
                        caption=make_caption("❌  <b>Upload Failed</b>", body),
                        reply_to_message_id=msg.message_id,
                    )
                    return

                # 3) Success — cache folder for future uploads
                new_folder_id = result.get("parentFolder")
                new_guest_token = result.get("guestToken")
                if new_folder_id and new_guest_token and not folder_id:
                    await save_user_folder(context, user.id, new_folder_id, new_guest_token)

                download_url = result.get("downloadPage", "https://gofile.io")
                share_code = result.get("parentFolderCode", "")

                # Update stats
                stats = context.bot_data.setdefault("stats", {"uploads": 0, "bytes": 0, "fails": 0})
                stats["uploads"] += 1
                stats["bytes"] += size_bytes

                # Delete status, send premium result card
                try:
                    await status_msg.delete()
                except Exception:
                    pass

                body = (
                    f"📁 <b>File:</b> <code>{safe_name}</code>\n"
                    f"💾 <b>Size:</b> <code>{size_str}</code>\n"
                    f"🎭 <b>Type:</b> <code>{html.escape(mime or 'unknown')}</code>\n"
                    f"🔗 <b>Link:</b> <a href=\"{download_url}\">{html.escape(share_code or download_url)}</a>\n"
                )
                if new_folder_id and folder_id:
                    body += "📦 <b>Added to your shared folder</b>\n"

                await bot_send_photo(
                    context,
                    chat_id=chat.id,
                    caption=make_caption("✨  <b>Upload Complete!</b>", body),
                    reply_markup=result_keyboard(download_url),
                    reply_to_message_id=msg.message_id,
                )

    except Exception as e:
        log.exception("Unhandled in handle_media")
        try:
            if status_msg:
                await status_msg.delete()
        except Exception:
            pass
        body = (
            f"📁 <b>File:</b> <code>{safe_name}</code>\n"
            f"\n❌ <b>Unexpected error:</b>\n<code>{html.escape(repr(e))[:300]}</code>"
        )
        await bot_send_photo(
            context,
            chat_id=chat.id,
            caption=make_caption("❌  <b>Error</b>", body),
            reply_to_message_id=msg.message_id,
        )
    finally:
        # Cleanup downloaded file
        if dest_path and dest_path.exists():
            try:
                dest_path.unlink()
            except Exception:
                pass


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "again":
        body = (
            "📤 <b>Ready for your next file!</b>\n"
            "Just send me any document, photo, video, or audio — I'll upload it instantly ⚡\n"
            "\n<i>Multiple files in this session share one folder link. 📦</i>"
        )
        await bot_send_photo(
            context,
            chat_id=update.effective_chat.id,
            caption=make_caption("🌸  <b>Upload Another</b>", body),
        )
    elif data == "dismiss":
        try:
            await q.message.delete()
        except Exception:
            pass
    elif data == "help":
        await cmd_help(update, context)
    elif data == "stats":
        await cmd_stats(update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception while handling update:", exc_info=context.error)
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    log.error(tb)
    # Notify dev chat if configured
    if DEV_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=DEV_CHAT_ID,
                text=f"⚠️ <b>Bot error</b>\n<pre>{html.escape(tb[-800:])}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Web server (serves the Mini App at /miniapp)
# ---------------------------------------------------------------------------
async def handle_miniapp(request: aiohttp_web.Request) -> aiohttp_web.Response:
    """Serve the Mini App HTML page."""
    if not MINIAPP_FILE.exists():
        return aiohttp_web.Response(
            text="Mini App file not found. Run from the project root.",
            status=500,
        )
    return aiohttp_web.Response(
        text=MINIAPP_FILE.read_text(encoding="utf-8"),
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "ALLOWALL",
        },
    )


async def handle_health(request: aiohttp_web.Request) -> aiohttp_web.Response:
    """Health check endpoint."""
    return aiohttp_web.Response(text='{"ok":true}', content_type="application/json")


async def handle_root(request: aiohttp_web.Request) -> aiohttp_web.Response:
    """Redirect / to /miniapp for convenience."""
    raise aiohttp_web.HTTPFound("/miniapp")


def build_web_app() -> aiohttp_web.Application:
    """Build the aiohttp web app that serves the Mini App."""
    web_app = aiohttp_web.Application()
    web_app.router.add_get("/", handle_root)
    web_app.router.add_get("/miniapp", handle_miniapp)
    web_app.router.add_get("/health", handle_health)
    return web_app


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------
_web_runner: Optional[aiohttp_web.AppRunner] = None


async def post_init(app: Application) -> None:
    global _web_runner
    app.bot_data["http"] = aiohttp.ClientSession(
        headers={"User-Agent": "GofileWaifuBot/1.0 (+https://gofile.io)"}
    )
    app.bot_data.setdefault("stats", {"uploads": 0, "bytes": 0, "fails": 0})
    load_waifu_pool()

    # Start the Mini App web server
    web_app = build_web_app()
    _web_runner = aiohttp_web.AppRunner(web_app)
    await _web_runner.setup()
    site = aiohttp_web.TCPSite(_web_runner, "0.0.0.0", WEB_PORT)
    await site.start()
    log.info("Mini App web server listening on http://0.0.0.0:%s/miniapp", WEB_PORT)
    if MINIAPP_URL:
        log.info("Mini App public URL: %s", MINIAPP_URL)
    else:
        log.warning(
            "MINIAPP_URL is not set — big-file (15 GB+) uploads disabled. "
            "Expose http://localhost:%s/miniapp via HTTPS (Cloudflare Tunnel / ngrok) "
            "and set MINIAPP_URL in .env to enable.",
            WEB_PORT,
        )

    # Initialize Telethon bridge (for files 20 MB → 2 GB / 4 GB)
    if TELETHON_AVAILABLE_IMPORT:
        try:
            await telethon_bridge.init_telethon()
            app.bot_data["telethon_ready"] = True
            log.info("Telethon bridge ready — files up to 2 GB (4 GB from Premium senders) supported.")
        except Exception as e:
            log.warning(
                "Telethon bridge failed to initialize: %s. "
                "Files > 20 MB will be rejected. Set API_ID and API_HASH in .env. "
                "Get them from https://my.telegram.org/apps",
                e,
            )
            app.bot_data["telethon_ready"] = False
    else:
        log.warning(
            "telethon_bridge module not available. "
            "Install requirements: pip install -r requirements.txt"
        )
        app.bot_data["telethon_ready"] = False

    me = await app.bot.get_me()
    log.info("Bot online: @%s (%s)", me.username, me.id)


async def post_shutdown(app: Application) -> None:
    global _web_runner
    session = app.bot_data.get("http")
    if session:
        await session.close()
    if _web_runner is not None:
        await _web_runner.cleanup()
        _web_runner = None
    # Close Telethon client
    if TELETHON_AVAILABLE_IMPORT and app.bot_data.get("telethon_ready"):
        await telethon_bridge.close_telethon()
    log.info("Bot shutting down. Bye! 🌸")


def main() -> None:
    if not BOT_TOKEN:
        print("=" * 60)
        print("ERROR: BOT_TOKEN not set!")
        print("  1. Copy .env.example to .env")
        print("  2. Get a token from @BotFather on Telegram")
        print("  3. Set BOT_TOKEN=your_token in .env")
        print("=" * 60)
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(8)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .read_timeout(30)
        .write_timeout(120)
        .connect_timeout(15)
        .pool_timeout(15)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("bigfile", cmd_bigfile))
    app.add_handler(CommandHandler("upload", cmd_bigfile))  # alias

    # Web App data (Mini App sends back the gofile URL)
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))

    # Media — catch every file-like message
    media_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.VOICE
        | filters.AUDIO
        | filters.ANIMATION
        | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(media_filter, handle_media))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(handle_button))

    # Error handler
    app.add_error_handler(on_error)

    log.info("=== Starting Gofile Waifu Bot ===")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import sys
    main()
