"""
Telethon bridge — downloads large files from Telegram via MTProto.

Why this exists:
  - python-telegram-bot's cloud Bot API caps file downloads at 20 MB.
  - This module uses Telethon (MTProto) to download files up to 2 GB
    (or 4 GB if the sender is a Telegram Premium user), with NO size cap.
  - Downloads in N-way PARALLEL chunks (default 8) — 5-10x faster than
    single-stream on most connections.
  - Uses os.pwrite() for positional disk writes (no seek, no lock, atomic).
  - Uses cryptg (C-based crypto) for ~10-50x faster decryption.

Lifecycle:
  - One global TelegramClient instance, started once at bot startup.
  - Logged in with the SAME bot token PTB uses.
  - ONLY used for downloads — PTB still receives all updates.
  - Bot tokens support multiple concurrent MTProto connections, so this is safe.

Parallel download architecture:
  - File is pre-allocated with truncate(size) so all workers can write
    at any offset without contention.
  - File is split into N roughly-equal byte ranges.
  - N async workers each call iter_download(offset=, limit=, request_size=)
    on their own range — Telethon opens a separate MTProto request per chunk.
  - Each worker has its OWN file descriptor and uses os.pwrite(fd, chunk, offset)
    to write at its current position. No seek, no lock, no GIL contention.
  - A separate asyncio task reports progress every 1s by reading the shared
    bytes_done counter (atomic in Python thanks to GIL).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Callable, Awaitable

from telethon import TelegramClient
from telethon.tl.custom import Message as TgMessage

log = logging.getLogger("waifu-bot.telethon")

# Module-level singleton client
_client: Optional[TelegramClient] = None
_init_lock = asyncio.Lock()

# Telegram MTProto hard limits (per upload.getFile docs)
#   - chunk must be multiple of 4096 bytes
#   - chunk must be ≤ 1 MB (1_048_576 bytes)
# We use 1 MB — the maximum allowed. Larger = fewer round-trips, better throughput.
CHUNK_SIZE = 1024 * 1024  # 1 MB


async def init_telethon() -> TelegramClient:
    """
    Initialize the global Telethon client (called once from post_init).
    Reads API_ID, API_HASH, BOT_TOKEN from environment.
    """
    global _client
    async with _init_lock:
        if _client is not None:
            return _client

        api_id = os.environ.get("API_ID", "").strip()
        api_hash = os.environ.get("API_HASH", "").strip()
        bot_token = os.environ.get("BOT_TOKEN", "").strip()

        if not api_id or not api_hash:
            raise RuntimeError(
                "API_ID and API_HASH must be set in .env to download files > 20 MB. "
                "Get them from https://my.telegram.org/apps"
            )
        if not bot_token:
            raise RuntimeError("BOT_TOKEN must be set to initialize Telethon.")

        data_dir = Path(
            os.environ.get("DATA_DIR", "").strip()
            or ("/data" if Path("/data").is_dir() else str(Path(__file__).resolve().parent))
        )
        session_path = data_dir / "bot_session"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError):
            session_path = Path("/tmp") / "waifu_bot_session"
        log.info("Initializing Telethon client (session=%s)...", session_path)

        # Verify cryptg is installed (10-50x faster decryption)
        try:
            import cryptg  # noqa: F401
            log.info("cryptg detected — accelerated decryption enabled.")
        except ImportError:
            log.warning(
                "cryptg NOT installed — falling back to pure-Python crypto. "
                "Downloads will be ~10-50x slower. Run: pip install cryptg"
            )

        # Tune the client for maximum download throughput
        client = TelegramClient(
            str(session_path),
            int(api_id),
            api_hash,
            connection_retries=10,
            retry_delay=1,
            request_retries=5,
            flood_sleep_threshold=60,
            receive_updates=False,
            ping_interval=60,
            timeout=30,
        )
        await client.start(bot_token=bot_token)
        me = await client.get_me()
        log.info("Telethon online as bot: id=%s username=@%s", me.id, me.username)

        _client = client
        return client


async def close_telethon() -> None:
    """Disconnect the global client (called from post_shutdown)."""
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None
        log.info("Telethon client disconnected.")


def is_available() -> bool:
    """Check if Telethon is configured (API_ID, API_HASH present)."""
    return bool(
        os.environ.get("API_ID", "").strip()
        and os.environ.get("API_HASH", "").strip()
    )


# ---------------------------------------------------------------------------
# Parallel download
# ---------------------------------------------------------------------------
async def download_large_file(
    chat_id: int,
    message_id: int,
    dest_path: Path,
    progress_cb: Optional[Callable[[int, int], Awaitable[None]]] = None,
    parallel: int = 8,
) -> int:
    """
    Download a file attached to a Telegram message via MTProto, using
    N-way parallel chunked transfer.

    Args:
        chat_id: Telegram chat ID where the message lives.
        message_id: The message ID containing the file.
        dest_path: Where to save the file on disk.
        progress_cb: Async callback(bytes_done, total) for progress reporting.
                     Called from a separate task every ~1s — never blocks downloads.
        parallel: Number of parallel workers (default 8).
                  Each worker opens its own MTProto request stream + its own fd.
                  Recommended: 8 for typical servers, 4 for low-bandwidth,
                  16 for fat pipes (Railway 8GB+ RAM).

    Returns:
        File size in bytes.

    Raises:
        RuntimeError: if Telethon is not initialized or media has no size.
        telethon.errors.*: on network/Telegram errors.
    """
    if _client is None:
        raise RuntimeError("Telethon not initialized — call init_telethon() first.")

    # Clamp parallel to sane range
    parallel = max(1, min(parallel, 16))

    log.info(
        "Telethon parallel download: chat=%s msg=%s -> %s (parallel=%d)",
        chat_id, message_id, dest_path, parallel,
    )
    start_time = time.monotonic()

    # Fetch the message (with the media)
    msg: TgMessage = await _client.get_messages(chat_id, ids=message_id)
    if msg is None or not msg.media:
        raise RuntimeError(f"No media found in chat={chat_id} msg={message_id}")

    # Use msg.file.size — most reliable across document/photo/video/etc.
    if msg.file is None or not msg.file.size:
        raise RuntimeError("Message media has no downloadable file or size is zero.")
    total_size = int(msg.file.size)
    log.info("  File size: %d bytes (%.2f MB)", total_size, total_size / 1048576)

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-allocate the file so workers can write at any offset without
    # contention or fragmentation. truncate() extends with zeros.
    with open(dest_path, "wb") as f:
        f.truncate(total_size)

    # Carve the file into `parallel` contiguous byte ranges.
    base_range = total_size // parallel
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for i in range(parallel):
        if i == parallel - 1:
            end = total_size
        else:
            end = cursor + base_range
        if cursor < end:
            ranges.append((cursor, end))
        cursor = end

    log.info("  Split into %d ranges: %s", len(ranges), [
        f"{s/1048576:.1f}-{e/1048576:.1f}MB" for s, e in ranges
    ])

    # Shared state (GIL makes simple int updates atomic in CPython)
    bytes_done = 0
    download_failed = False
    first_error: Optional[BaseException] = None

    async def progress_reporter() -> None:
        """Standalone task: report progress every 1s until cancelled."""
        if not progress_cb:
            return
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    await progress_cb(bytes_done, total_size)
                except Exception:
                    pass  # never let progress break the download
        except asyncio.CancelledError:
            try:
                await progress_cb(bytes_done, total_size)
            except Exception:
                pass
            raise

    async def worker(worker_idx: int, range_start: int, range_end: int) -> None:
        """Download one byte range using its own MTProto stream + its own fd."""
        nonlocal bytes_done, download_failed, first_error
        if download_failed:
            return  # short-circuit if another worker already failed

        # Each worker opens its OWN file descriptor. os.pwrite() is
        # positional + atomic — no seek, no lock, multiple fds safe.
        fd = os.open(str(dest_path), os.O_WRONLY)
        try:
            offset = range_start
            async for chunk in _client.iter_download(
                msg.media,
                offset=range_start,
                limit=range_end - range_start,
                request_size=CHUNK_SIZE,
            ):
                if not chunk:
                    break
                if download_failed:
                    return  # short-circuit
                os.pwrite(fd, chunk, offset)
                offset += len(chunk)
                bytes_done += len(chunk)
        except Exception as e:
            if not download_failed:
                download_failed = True
                first_error = e
                log.error("  Worker %d failed at offset %d: %s", worker_idx, offset, e)
        finally:
            os.close(fd)

    # Launch progress reporter + all workers concurrently
    reporter = asyncio.create_task(progress_reporter())
    try:
        await asyncio.gather(*[
            worker(i, s, e) for i, (s, e) in enumerate(ranges)
        ])
    finally:
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass

    # Final progress update
    if progress_cb:
        try:
            await progress_cb(bytes_done, total_size)
        except Exception:
            pass

    if download_failed:
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        assert first_error is not None
        raise first_error

    elapsed = time.monotonic() - start_time
    speed_mbps = (bytes_done / 1048576) / elapsed if elapsed > 0 else 0
    log.info(
        "  Download complete: %d bytes in %.2fs (%.2f MB/s, %d-way parallel)",
        bytes_done, elapsed, speed_mbps, parallel,
    )
    return bytes_done


def get_filename_from_message(msg: TgMessage) -> str:
    """Extract a sensible filename from a Telethon message."""
    if msg.file and msg.file.name:
        return msg.file.name
    if msg.document and msg.document.attributes:
        for attr in msg.document.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                return attr.file_name
    ext = ""
    if msg.photo:
        ext = ".jpg"
    elif msg.video:
        ext = ".mp4"
    elif msg.voice:
        ext = ".ogg"
    elif msg.audio:
        ext = ".mp3"
    elif msg.video_note:
        ext = ".mp4"
    return f"file_{msg.id}{ext}"
