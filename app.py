"""xmedia-web: a tiny website that downloads images/videos from X (Twitter) posts."""
import asyncio
import logging
import os
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# --- Rate limiting ---------------------------------------------------------
# Limits are per client IP. Tweak these to taste.
LOOKUP_LIMIT = "20/minute"
DOWNLOAD_LIMIT = "10/minute"

# NOTE: behind a reverse proxy (nginx, Caddy, Fly, etc.) every request looks like
# it comes from the proxy's IP unless you run uvicorn with:
#   --proxy-headers --forwarded-allow-ips="<your proxy IP>"
limiter = Limiter(key_func=get_remote_address)

log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn terminal

app = FastAPI(title="Xitter Downloader")
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limited(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        {"detail": "Too many requests. Give it a minute and try again."},
        status_code=429,
        headers={"Retry-After": "60"},
    )


# --- Config ----------------------------------------------------------------
TWEET_RE = re.compile(r"(?:twitter|x)\.com/([^/]+)/status/(\d+)")
ALLOWED_HOSTS = {"video.twimg.com"}  # only proxy X's video CDN

# Bandwidth guards (override with environment variables)
MAX_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "100")) * 1024 * 1024
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5")))


# --- Routes ----------------------------------------------------------------
# --- Media lookup: FxTwitter first, yt-dlp as fallback ------------------------
PHOTOS_ONLY = "photos_only"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; XitterDownloader/1.0)"}


def pick_videos(tweet: dict):
    """Videos/GIFs from the tweet itself, or from the tweet it quotes."""
    for source in (tweet, tweet.get("quote") or {}):
        items = (source.get("media") or {}).get("all", [])
        videos = [i for i in items if i.get("type") in ("video", "gif") and i.get("url")]
        if videos:
            return videos, bool(items)
    own = (tweet.get("media") or {}).get("all", [])
    return [], bool(own)


async def lookup_fxtwitter(user: str, tweet_id: str):
    """Primary source. Returns (author, media), PHOTOS_ONLY, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS) as client:
            r = await client.get(f"https://api.fxtwitter.com/{user}/status/{tweet_id}")
        if r.status_code != 200:
            log.warning("FxTwitter returned HTTP %s for %s/%s", r.status_code, user, tweet_id)
            return None
        tweet = r.json().get("tweet") or {}
        videos, has_any_media = pick_videos(tweet)
        if not videos:
            if has_any_media:
                return PHOTOS_ONLY
            log.warning("FxTwitter found no media. Tweet keys: %s", sorted(tweet.keys()))
            return None
        return (
            (tweet.get("author") or {}).get("screen_name", user),
            [
                {
                    "type": i["type"],  # video | gif
                    "url": i["url"],
                    "thumb": i.get("thumbnail_url") or "",
                }
                for i in videos
            ],
        )
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("FxTwitter lookup failed: %r", e)
        return None


def best_mp4(info: dict):
    """Pick the best progressive mp4 (not HLS) from a yt-dlp info dict."""
    candidates = [
        f for f in info.get("formats") or []
        if f.get("url")
        and f.get("ext") == "mp4"
        and f.get("vcodec") not in (None, "none")
        and f.get("protocol") in ("https", "http")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))


def ytdlp_extract(url: str):
    """Blocking yt-dlp call (run in a thread). Returns (author, media list) or None."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("yt-dlp failed: %s", e)
        return None
    entries = info.get("entries") or [info]
    media = []
    for e in entries:
        fmt = best_mp4(e)
        if fmt:
            media.append({"type": "video", "url": fmt["url"], "thumb": e.get("thumbnail") or ""})
    if not media:
        log.warning("yt-dlp found no progressive mp4 formats")
        return None
    return info.get("uploader_id") or info.get("uploader") or "unknown", media


@app.get("/api/media")
@limiter.limit(LOOKUP_LIMIT)
async def media(request: Request, url: str = Query(..., description="Tweet URL")):
    m = TWEET_RE.search(url)
    if not m:
        raise HTTPException(400, "That doesn't look like a tweet URL.")
    user, tweet_id = m.groups()

    result = await lookup_fxtwitter(user, tweet_id)
    if result == PHOTOS_ONLY:
        raise HTTPException(404, "That post only has photos. This site is for videos and GIFs.")
    if result is None:
        # Fallback if FxTwitter is down or failed.
        canonical = f"https://x.com/{user}/status/{tweet_id}"
        try:
            result = await asyncio.wait_for(asyncio.to_thread(ytdlp_extract, canonical), timeout=45)
        except asyncio.TimeoutError:
            result = None
    if result is None:
        raise HTTPException(404, "Couldn't find any media for that tweet (deleted, private, or no video).")

    author, items = result
    return {"author": author, "media": items}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/download")
@limiter.limit(DOWNLOAD_LIMIT)
async def download(request: Request, url: str):
    """Stream a video through our server so the browser saves it instead of playing it."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise HTTPException(400, "Only X video URLs are allowed.")
    filename = PurePosixPath(parsed.path).name or "video.mp4"

    if slots.locked():
        raise HTTPException(503, "Busy right now. Try again in a moment.")
    await slots.acquire()

    client = httpx.AsyncClient(timeout=30)
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        slots.release()
        raise HTTPException(502, "Couldn't reach the video server.")

    length = int(upstream.headers.get("content-length") or 0)
    if upstream.status_code != 200 or length > MAX_BYTES:
        await upstream.aclose()
        await client.aclose()
        slots.release()
        if upstream.status_code == 200:
            raise HTTPException(413, f"That video is over the {MAX_BYTES // 1024 // 1024} MB limit.")
        raise HTTPException(502, "Video server refused the request.")

    async def body():
        sent = 0
        try:
            async for chunk in upstream.aiter_bytes(65536):
                sent += len(chunk)
                if sent > MAX_BYTES:  # safety net when Content-Length is missing
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            slots.release()

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if length:
        headers["Content-Length"] = str(length)
    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=headers,
    )


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
