# syntax=docker/dockerfile:1.6
# ============================================================================
#  Gofile Waifu Bot — production Docker image
#  Optimized for Railway one-click deploy (also works on Fly.io, Render,
#  any container host that injects PORT and provides a public domain).
# ============================================================================
FROM python:3.12-slim AS base

# ---- 1. System deps --------------------------------------------------------
#   gcc / g++ / libc6-dev  → build `cryptg` (C-extension for Telethon, 10-50x
#                            faster MTProto decryption — without it Telethon
#                            falls back to pure-Python crypto at ~100 KB/s)
#   curl                   → used by HEALTHCHECK
#   ca-certificates        → TLS for outbound HTTPS (gofile, Telegram, nekos.best)
#   tini                   → tiny init that reaps zombies and forwards signals
#                            (so SIGTERM from Railway triggers graceful shutdown)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libc6-dev \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- 2. Install Python deps FIRST (better layer caching) -------------------
# Requirements rarely change, so this layer stays cached across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 3. Copy app code + static assets --------------------------------------
#   .dockerignore excludes: .git, .env, downloads/, logs/, *.session, etc.
COPY . .

# ---- 4. Persistent data directory ------------------------------------------
# `/data` is the conventional Railway volume mount path.
# Locally / on hosts without a volume, the bot auto-falls back to /tmp.
RUN mkdir -p /data /tmp/waifu-bot \
    && chmod 777 /data /tmp/waifu-bot

# ---- 5. Non-root user (Railway / most hosts prefer non-root) ---------------
RUN useradd --create-home --shell /bin/bash --uid 1000 waifu \
    && chown -R waifu:waifu /app /data /tmp/waifu-bot
USER waifu

# ---- 6. Env defaults -------------------------------------------------------
# Railway auto-injects PORT and RAILWAY_PUBLIC_DOMAIN at runtime.
# These defaults only apply when running outside Railway (local docker run).
ENV WEB_PORT=8080 \
    DATA_DIR=/data

# ---- 7. Healthcheck --------------------------------------------------------
# Railway will hit /health every 30s; 3 consecutive failures trigger a restart.
# We use curl instead of python -c to keep the check fast (~10ms vs ~300ms).
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-${WEB_PORT}}/health" || exit 1

# ---- 8. Entrypoint ---------------------------------------------------------
# tini → init, signal forwarding, zombie reaping
# python bot.py → the bot itself (PTB polling + aiohttp web server in-process)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]

# ---- 9. Metadata -----------------------------------------------------------
LABEL org.opencontainers.image.title="Gofile Waifu Bot" \
      org.opencontainers.image.description="Telegram bot that uploads any file to gofile.io with cute anime girl images" \
      org.opencontainers.image.source="https://github.com/yourusername/gofile-waifu-bot" \
      org.opencontainers.image.licenses="MIT"

# Railway injects PORT at runtime — expose it for documentation only.
EXPOSE 8080
