"""
Refresh the local waifu image pool by downloading fresh SFW anime girl images
from verified public APIs.

Primary:   nekos.best/api/v2/{neko|waifu|kitsune}   (200 req/min, artist credit)
Fallback:  nekos.life/api/v2/img/{neko|waifu|fox_girl}
Last resort: api.purrbot.site/v2/img/sfw/neko/img

Usage:
    python refresh_waifus.py            # default 30 images
    python refresh_waifus.py 50         # 50 images
"""
import asyncio
import aiohttp
import json
import random
from pathlib import Path

SAVE_DIR = Path(__file__).resolve().parent / "assets" / "waifus"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

NEKOS_BEST_CATEGORIES = ["neko", "waifu", "kitsune"]
NEKOS_LIFE_CATEGORIES = ["neko", "waifu", "fox_girl"]


async def fetch_nekos_best(session, category):
    url = f"https://nekos.best/api/v2/{category}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()
    item = data["results"][0]
    return item["url"], item.get("artist_name"), item.get("artist_href")


async def fetch_nekos_life(session, category):
    url = f"https://nekos.life/api/v2/img/{category}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()
    return data["url"], None, None


async def fetch_purrbot(session):
    url = "https://api.purrbot.site/v2/img/sfw/neko/img"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()
    return data["link"], None, None


async def get_one_image_url(session):
    cat = random.choice(NEKOS_BEST_CATEGORIES)
    try:
        return await fetch_nekos_best(session, cat)
    except Exception as e:
        print(f"  [warn] nekos.best/{cat} failed: {e}")
    cat = random.choice(NEKOS_LIFE_CATEGORIES)
    try:
        return await fetch_nekos_life(session, cat)
    except Exception as e:
        print(f"  [warn] nekos.life/{cat} failed: {e}")
    return await fetch_purrbot(session)


async def download_one(session, idx):
    try:
        img_url, artist, artist_href = await get_one_image_url(session)
    except Exception as e:
        print(f"[{idx:02d}] Failed to get URL: {e}")
        return None

    lower = img_url.lower().split("?")[0]
    if lower.endswith(".png"):
        ext = ".png"
    elif lower.endswith((".jpg", ".jpeg")):
        ext = ".jpg"
    elif lower.endswith(".webp"):
        ext = ".webp"
    elif lower.endswith(".gif"):
        ext = ".gif"
    else:
        ext = ".jpg"

    filename = f"waifu_{idx:03d}{ext}"
    dest = SAVE_DIR / filename

    try:
        async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            r.raise_for_status()
            data = await r.read()
        dest.write_bytes(data)
        size_kb = len(data) / 1024
        credit = f" (art: {artist})" if artist else ""
        print(f"[{idx:02d}] OK {filename}  {size_kb:.0f} KB{credit}")
        return {
            "file": filename,
            "source_url": img_url,
            "artist": artist,
            "artist_href": artist_href,
        }
    except Exception as e:
        print(f"[{idx:02d}] Failed to download {img_url}: {e}")
        return None


async def main():
    target = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 30
    print(f"=== Downloading {target} cute anime girl images ===")
    print(f"Save dir: {SAVE_DIR}\n")

    headers = {"User-Agent": "WaifuBot/1.0 (Telegram bot; image pool bootstrap)"}
    conn = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
        batch = 5
        results = []
        for start in range(0, target, batch):
            tasks = [download_one(session, start + i) for i in range(batch)]
            batch_results = await asyncio.gather(*tasks)
            results.extend([r for r in batch_results if r])

    print(f"\n=== Done. {len(results)}/{target} images downloaded ===")
    manifest_path = SAVE_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"Manifest saved: {manifest_path}")

    pool = sorted(SAVE_DIR.glob("waifu_*"))
    print(f"\nFinal pool size: {len(pool)} images")


if __name__ == "__main__":
    asyncio.run(main())
