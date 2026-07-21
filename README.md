# 🌸 Gofile Waifu Bot

A premium Telegram bot that uploads any file to **gofile.io** and gives you a fast shareable link — with a **cute anime girl image attached to every reply**! 🎀

> **One-click Railway deploy available** — see [🚂 Railway Deploy (one click)](#-railway-deploy-one-click) below.

## 🚂 Railway Deploy (one click)

The fastest way to get this bot live. Free Railway trial covers ~500 hours/month — plenty for testing.

### What you'll need (5 minutes total)

| Item | Where to get it | Used for |
|------|-----------------|----------|
| Telegram bot token | [@BotFather](https://t.me/BotFather) → `/newbot` | Bot identity |
| `API_ID` + `API_HASH` | [my.telegram.org/apps](https://my.telegram.org/apps) | Files > 20 MB (Telethon MTProto) |
| Railway account | [railway.app](https://railway.app) | Hosting |
| GitHub account | Push this repo to your GitHub | Railway builds from there |

### Step-by-step

1. **Push this project to your GitHub** (or fork it):
   ```bash
   git init && git add . && git commit -m "init"
   git remote add origin https://github.com/YOUR_USERNAME/gofile-waifu-bot.git
   git push -u origin main
   ```

2. **Open [railway.app/new](https://railway.app/new)** → pick your GitHub repo.

3. Railway auto-detects the `Dockerfile` and `railway.json` → **Deploy** button becomes clickable. Click it.

4. The first build takes ~2 minutes (installs `telethon` + `cryptg` + PTB). Watch the **Deploy Logs** tab.

5. While it builds, go to **Settings → Variables** and add:
   ```
   BOT_TOKEN  =  123456789:ABC...        (from @BotFather)
   API_ID     =  12345                    (from my.telegram.org)
   API_HASH   =  abc123def456...          (from my.telegram.org)
   ```
   Optional: `DEV_CHAT_ID`, `GOFILE_TOKEN` (see [.env.example](.env.example)).

6. Go to **Settings → Networking** → toggle **Public Networking** ON.
   - Railway generates a public URL like `https://gofile-waifu-bot-production.up.railway.app`
   - The bot auto-detects this via `RAILWAY_PUBLIC_DOMAIN` and configures `MINIAPP_URL` for you. ✨
   - **No Cloudflare Tunnel / ngrok needed.**

7. (Optional but recommended) **Settings → Volumes → Add Volume**:
   - Mount path: `/data`
   - This persists the Telethon session across restarts (avoids "new login" Telegram alerts).

8. Wait for the healthcheck to pass (green dot). Check **Deploy Logs** for:
   ```
   === Starting Gofile Waifu Bot ===
   Mini App web server listening on http://0.0.0.0:PORT/miniapp
   Auto-detected MINIAPP_URL from RAILWAY_PUBLIC_DOMAIN: https://xxx.up.railway.app/miniapp
   Telethon bridge ready — files up to 2 GB (4 GB from Premium senders) supported.
   Bot online: @your_bot_username (123456789)
   ```

9. Open your bot in Telegram → tap **Start** → send a file. Done! 🌸

### Why this works on Railway without extra setup

| Concern | Solution |
|---------|----------|
| Telegram needs HTTPS for Mini App | Railway auto-provides a public HTTPS domain via `RAILWAY_PUBLIC_DOMAIN` |
| Container needs a port | Railway auto-injects `PORT` env var — bot reads it |
| `cryptg` C-extension needs gcc | Dockerfile installs `gcc g++ libc6-dev` at build time |
| Ephemeral filesystem (session/logs lost on restart) | Bot uses `/data` volume if mounted, falls back to `/tmp` otherwise |
| Need to know if bot is alive | `/health` endpoint + Dockerfile `HEALTHCHECK` + Railway auto-restart on failure |
| Graceful shutdown on `SIGTERM` | `tini` init forwards signals → PTB's `run_polling` cleanly disconnects |
| Non-root user for security | Dockerfile creates `waifu` user (UID 1000) |

### Railway cost estimate

- **Free trial**: $5 credit → ~500 hours of single-container hosting (enough for testing)
- **Hobby plan** ($5/month): 500 hours included, then $0.000463/min (~$0.02/hour)
- **For a single-bot workload**: ~$5-8/month typical

### Troubleshooting Railway deploys

<details>
<summary><b>Build fails with "gcc not found" / "cryptg failed to build"</b></summary>

The Dockerfile installs `gcc g++ libc6-dev` for this exact reason. If you're using Nixpacks instead of Dockerfile:
- Go to **Settings → Build → Builder** → switch from `NIXPACKS` to `DOCKERFILE`
- Or add a `nixpacks.toml` with `aptPkgs = ["gcc", "g++", "libc6-dev"]`
</details>

<details>
<summary><b>Healthcheck fails (red dot, constant restarts)</b></summary>

1. Check **Deploy Logs** for the actual error
2. Common causes:
   - `BOT_TOKEN` not set → bot exits immediately
   - `API_ID` is a string like `"12345"` (it should be just `12345`)
   - `API_HASH` wrong length (must be 32 hex chars)
3. The bot binds to `0.0.0.0:$PORT` — never `127.0.0.1` (Railway requires `0.0.0.0`)
</details>

<details>
<summary><b>Mini App doesn't open in Telegram (infinite spinner)</b></summary>

- Make sure **Public Networking** is ON in Railway service settings
- Verify `RAILWAY_PUBLIC_DOMAIN` shows up in your service's **Variables** tab (auto-generated)
- Open the URL in a browser directly — should show the upload UI
- The URL must be HTTPS (Railway always is)
</details>

<details>
<summary><b>Want to use Nixpacks instead of Dockerfile?</b></summary>

Delete or rename the `Dockerfile`, then create `nixpacks.toml`:
```toml
[phases.setup]
aptPkgs = ["gcc", "g++", "libc6-dev", "curl", "ca-certificates"]

[phases.install]
cmds = ["pip install --no-cache-dir -r requirements.txt"]

[start]
cmd = "python bot.py"
```
Railway auto-detects Python via Nixpacks and applies these overrides.
</details>

<details>
<summary><b>Local Docker testing before pushing to Railway</b></summary>

```bash
# Build
docker build -t waifu-bot .

# Run (set your env vars first)
docker run -p 8080:8080 \
  -e BOT_TOKEN=123:ABC \
  -e API_ID=12345 \
  -e API_HASH=abc... \
  -e PORT=8080 \
  -e RAILWAY_PUBLIC_DOMAIN=mybot.example.com \
  -v $(pwd)/.data:/data \
  waifu-bot

# Healthcheck
curl http://localhost:8080/health
# -> {"ok":true}
```
</details>

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🚀 **THREE upload paths** | (1) Small files (≤20 MB) via Bot API. (2) Medium files (20 MB → 2 GB / 4 GB) via **Telethon MTProto bridge** (fast, parallel). (3) Any-size files (15 GB+) via the Mini App (browser-direct to gofile) |
| ⚡ **Fast streaming uploads** | Multi-region gofile endpoints (EU/NA/AP), low RAM, true async streaming |
| 🏎 **Telethon MTProto downloads** | `cryptg` C-based crypto → 10-50x faster decryption, no 20 MB wall, no extra Docker service |
| 🎨 **Premium UI** | HTML-formatted messages with inline keyboards (Open / Copy / Upload Another) |
| 🖼 **Cute waifu every reply** | 30-image pre-downloaded pool + live SFW API fallback — different image per message |
| 📦 **Batch uploads** | Multiple small files from same user → same shared folder link |
| 🚦 **Concurrency control** | Per-user (2) + global (8) semaphores |
| 📊 **Live progress** | Real-time progress bars for both download AND upload phases |
| 🔒 **Privacy** | Guest uploads by default (no signup needed) |
| 🛡 **Resilient** | Retry across regional endpoints, graceful errors, dev-chat alerts |
| 🌐 **Built-in Mini App server** | aiohttp web server — no separate hosting needed |

## 📦 The Three Upload Paths Explained

| Path | File size range | Method | Speed |
|------|----------------|--------|-------|
| **1. Direct** | 0 – 20 MB | PTB cloud Bot API → gofile.io | ⚡⚡⚡ instant |
| **2. Telethon** | 20 MB – 2 GB (4 GB if sender Premium) | MTProto download → gofile.io | ⚡⚡ fast (parallel chunks, cryptg acceleration) |
| **3. Mini App** | Any size (15 GB+ works) | Browser → gofile.io directly (CORS-verified) | ⚡⚡⚡ fastest (no Telegram in the loop) |

### Path 1: Direct small-file upload (≤20 MB)

Just send any file (document, photo, video, audio, voice, GIF, video note) to the bot in the chat. The bot downloads it from Telegram via the cloud Bot API, streams it to gofile.io, and replies with the shareable link + a cute waifu image.

### Path 2: Telethon MTProto bridge (20 MB → 2 GB / 4 GB)

When you send a file between 20 MB and 2 GB (or 4 GB if you're a Telegram Premium user), the bot automatically switches to **Telethon** — a Python MTProto client — to download the file from Telegram's servers. MTProto has no 20 MB cap, and with `cryptg` (C-based crypto accelerator) installed, downloads are 10-50x faster than pure-Python alternatives.

The bot then uploads the file to gofile.io with a live progress bar.

**Setup required:** `API_ID` and `API_HASH` in `.env` (see [Setup](#-setup)).

### Path 3: Mini App big-file upload (any size — 15 GB+)

Tap the **"📦 Upload Big File"** button in the bot. A browser page opens inside Telegram where you pick any file. The browser uploads it **directly to gofile.io** (CORS-verified) — the file never touches Telegram's servers, so Telegram's 2 GB / 4 GB storage wall doesn't apply. The link is then sent back to your Telegram chat automatically.

**Setup required:** `MINIAPP_URL` in `.env` (Cloudflare Tunnel / ngrok — see below).

## 🚀 Setup

> **Quickest path:** Railway one-click deploy — see [🚂 Railway Deploy](#-railway-deploy-one-click) at the top. ~5 minutes, no local install needed.
>
> **Local dev / self-hosting:** follow the steps below.

### Step 1: Get a bot token

1. Open Telegram → message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Follow the prompts → copy the bot token

### Step 2: Get API_ID and API_HASH (for files > 20 MB)

These are **per-developer, not per-bot** — you get them once and reuse for all bots.

1. Go to https://my.telegram.org → sign in with your phone number
2. Click "API development tools"
3. Fill in any app title + short name (doesn't matter what)
4. Copy `api_id` (a number) and `api_hash` (a long hex string)

### Step 3: Install

```bash
cd gofile-waifu-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 4: Configure

```bash
cp .env.example .env
```

Edit `.env` and set:
```ini
BOT_TOKEN=123456:your_bot_token_here
API_ID=12345                      # from my.telegram.org
API_HASH=abcdef1234567890abcdef1234567890  # from my.telegram.org
```

At this point both Path 1 (small files) and Path 2 (medium files up to 2 GB / 4 GB) work. To enable Path 3 (15 GB+ Mini App), continue to Step 5.

### Step 5: Expose the Mini App via HTTPS (for 15 GB+ uploads)

The bot serves the Mini App on `http://localhost:8080/miniapp`. Telegram requires HTTPS, so pick one of:

#### Option A: Cloudflare Tunnel (recommended — free, no signup)

```bash
# Install cloudflared: https://github.com/cloudflare/cloudflared/releases
cloudflared tunnel --url http://localhost:8080
```

You'll see output like:
```
Your quick Tunnel has been created! Visit it at:
  https://random-words-1234.trycloudflare.com
```

Copy that URL and set in `.env`:
```ini
MINIAPP_URL=https://random-words-1234.trycloudflare.com/miniapp
```

#### Option B: ngrok

```bash
ngrok http 8080
# Copy the HTTPS URL → set MINIAPP_URL=https://xxxx.ngrok-free.app/miniapp
```

#### Option C: Your own reverse proxy

Point nginx/Caddy/etc. at `http://localhost:8080` and set `MINIAPP_URL` to your public HTTPS URL + `/miniapp`.

### Step 6: Run

```bash
python bot.py
```

You should see:
```
=== Starting Gofile Waifu Bot ===
Mini App web server listening on http://0.0.0.0:8080/miniapp
Mini App public URL: https://your-tunnel-url/miniapp
Telethon bridge ready — files up to 2 GB (4 GB from Premium senders) supported.
Bot online: @your_bot_username (123456789)
```

Now open your bot in Telegram, hit **Start**, and:
- Send a small file (≤20 MB) → instant gofile link via Bot API
- Send a medium file (20 MB – 2 GB / 4 GB) → fast Telethon download → gofile link
- Tap **"📦 Upload Big File"** → Mini App opens → any size file → link appears in chat 🌸

## 📁 Project Structure

```
gofile-waifu-bot/
├── bot.py                  # Main bot (PTB + web server + Telethon integration)
├── telethon_bridge.py      # MTProto download bridge (2 GB / 4 GB files)
├── refresh_waifus.py       # Re-download waifu image pool
├── requirements.txt        # Python deps (incl. telethon, cryptg)
├── .env.example            # Config template
├── .gitignore
├── Dockerfile              # Production container image (Railway / Docker / Fly.io)
├── .dockerignore           # Files excluded from the Docker image
├── railway.json            # Railway service config (builder, healthcheck, restart policy)
├── README.md
├── miniapp/
│   └── index.html          # Premium Mini App (single HTML file)
├── assets/
│   └── waifus/             # 30 pre-downloaded SFW anime girl images
│       ├── waifu_000.png ... waifu_029.png
│       └── manifest.json   # Image metadata (artist credits)
├── downloads/              # Temp file staging (auto-cleaned; /data on Railway)
├── logs/                   # Bot logs (stdout on Railway; /data/logs if volume mounted)
└── bot_session.session     # Telethon session (auto-created; /data/bot_session on Railway)
```

## ⚙️ Configuration (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BOT_TOKEN` | ✅ Yes | — | Telegram bot token from @BotFather |
| `API_ID` | ✅ For >20 MB | — | Telegram API ID from my.telegram.org/apps |
| `API_HASH` | ✅ For >20 MB | — | Telegram API hash from my.telegram.org/apps |
| `DEV_CHAT_ID` | ❌ Optional | — | Your Telegram user ID for error alerts |
| `GOFILE_TOKEN` | ❌ Optional | — | Gofile account token (default: guest uploads) |
| `MINIAPP_URL` | ❌ For 15 GB+ | auto on Railway | Public HTTPS URL of the Mini App. Auto-set from `RAILWAY_PUBLIC_DOMAIN`. |
| `WEB_PORT` | ❌ Optional | `8080` / `$PORT` | Local HTTP port. Railway auto-injects `PORT`. |
| `TELETHON_PARALLEL_CHUNKS` | ❌ Optional | `3` | Parallel chunk downloaders (1=low RAM, 5+=fast server) |
| `DATA_DIR` | ❌ Optional | `/data` if mounted | Persistent data dir (downloads, logs, Telethon session). Auto-detected. |

**Railway-injected (don't set these yourself):**
| Variable | Description |
|----------|-------------|
| `PORT` | TCP port Railway exposes — bot reads this automatically |
| `RAILWAY_PUBLIC_DOMAIN` | HTTPS domain Railway generates when Public Networking is ON — bot auto-configures `MINIAPP_URL` from this |
| `RAILWAY_ENVIRONMENT` | Set to `production` by Railway — used by some libs for telemetry |

## 🛠 Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + buttons |
| `/help` | Show help |
| `/stats` | Show upload statistics |
| `/bigfile` | Open the big-file Mini App uploader directly |
| `/upload` | Alias for `/bigfile` |
| `/cancel` | Cancel an in-progress upload |

## 📊 How it works

### Small file (≤20 MB) flow

```
User sends file (≤20 MB) in chat
    ↓
Bot downloads via PTB cloud Bot API (streamed)
    ↓
Bot streams to gofile.io via multi-region endpoint
    ↓ (with live progress bar)
Gofile returns shareable URL: https://gofile.io/d/XXXXXX
    ↓
Bot replies with: waifu image + premium caption + inline buttons
```

### Medium file (20 MB → 2 GB / 4 GB) flow — NEW

```
User sends file (20 MB – 2 GB / 4 GB) in chat
    ↓
Bot detects size > 20 MB → switches to Telethon MTProto bridge
    ↓
Telethon downloads in parallel 1 MB chunks (cryptg-accelerated decryption)
    ↓ (with live download progress bar)
Bot streams file to gofile.io via multi-region endpoint
    ↓ (with live upload progress bar)
Gofile returns shareable URL: https://gofile.io/d/XXXXXX
    ↓
Bot replies with: waifu image + premium caption + inline buttons
```

### Big file (any size, 15 GB+) flow

```
User taps "Upload Big File" button (InlineKeyboardButton with web_app)
    ↓
Telegram opens the Mini App (HTTPS web page) in-app
    ↓
User picks file via <input type=file> (drag-drop supported)
    ↓
Browser does fetch POST multipart to https://upload.gofile.io/uploadfile
    (CORS-verified: Access-Control-Allow-Origin: *)
    (XHR.upload.onprogress → live %, speed, ETA)
    ↓
Gofile returns JSON {data: {downloadPage: "https://gofile.io/d/XXXXXX"}}
    ↓
Mini App calls Telegram.WebApp.sendData(JSON) — sends URL back to bot
    ↓
Bot receives Message.web_app_data → posts link + waifu image to chat
```

## 🎨 Waifu Image Sources

All images are **SFW (safe for work)**. Sources (live-verified):

1. **nekos.best** — Primary. SFW-only, 200 req/min, includes artist credit.
2. **nekos.life** — Fallback. Simple, fast, tiny images.
3. **purrbot.site v2** — Last resort.

The bot uses a pre-downloaded pool of 30 images (with artist credits in `assets/waifus/manifest.json`) and falls back to live API calls if the pool is empty.

To refresh the pool:
```bash
python refresh_waifus.py            # default 30 images
python refresh_waifus.py 50         # 50 images
```

## 🔧 Troubleshooting

### Bot says "Large file needs setup" when I send a 50 MB file

You forgot to set `API_ID` and `API_HASH` in `.env`. Get them from https://my.telegram.org/apps (sign in with phone → "API development tools" → create app). Add to `.env` and restart the bot.

### Telethon bridge failed to initialize

Check `logs/bot.log` for the exact error. Common causes:
- `API_ID` is not a valid integer (must be a number like `12345`, not a string)
- `API_HASH` is wrong length (should be 32 hex characters)
- Network can't reach Telegram's MTProto servers (check firewall)

### Bot says "Use the Big-File Uploader" when I send a 5 GB file

That's correct! Telegram's hard limit is 4 GB (Premium senders only). Files larger than 4 GB **cannot exist on Telegram** — you must use the Mini App which bypasses Telegram entirely.

### Mini App doesn't open

- Check `MINIAPP_URL` is set in `.env` and points to a working HTTPS URL
- Verify the URL by opening it in your browser — you should see the premium upload UI
- Check `cloudflared` / `ngrok` is still running (free tunnels close when the process dies)

### Small-file upload fails

- Check file is under 20 MB (Telegram bot API limit)
- Check `logs/bot.log` for details
- Gofile may be temporarily down — retry in a moment

### Download speed is slow even with Telethon

Make sure `cryptg` is installed:
```bash
pip install cryptg
```
Without `cryptg`, Telethon falls back to pure-Python crypto (~100 KB/s). With `cryptg`, you should see 5-50 MB/s depending on your network and Telegram's datacenter.

You can also increase `TELETHON_PARALLEL_CHUNKS` in `.env` (default 3) for faster downloads on a fast server with good bandwidth.

## 📜 License

MIT — do whatever you want. Just don't blame me if your waifu leaves you.

## 🌸 Credits

- **Gofile** — https://gofile.io
- **Anime art** — All artists credited in `assets/waifus/manifest.json`
- **nekos.best** / **nekos.life** / **purrbot.site** — Image APIs
- **python-telegram-bot** — https://docs.python-telegram-bot.org
- **Telethon** — https://docs.telethon.dev
- **cryptg** — C-based AES for Telethon
- **Telegram WebApp SDK** — https://core.telegram.org/bots/webapps

---

Made with 💜 and lots of 🌸
