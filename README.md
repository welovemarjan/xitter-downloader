# Xitter Downloader

A minimal, open source website for saving videos and GIFs from X (Twitter) posts.
Paste a post link, get a download. No accounts, no ads, no tracking.

> Videos and GIFs only. Photos can already be saved from X directly.

## How it works

- `/api/media` looks the post up through [FxTwitter](https://github.com/FixTweet/FxTwitter). If that fails, it falls back to [yt-dlp](https://github.com/yt-dlp/yt-dlp).
- `/api/download` streams the video through the server so the browser saves it instead of playing it. It only accepts `video.twimg.com` URLs.
- Per-IP rate limits, a per-file size cap, and a cap on simultaneous downloads protect the server's bandwidth.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://localhost:8000.

## Run with Docker

```bash
docker build -t xitter-downloader .
docker run -p 8000:8000 xitter-downloader
```

## Deploy to Render (free tier)

1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint** and select the repo. It reads `render.yaml`.
3. Deploy.

Free instances sleep after 15 minutes of inactivity (the first request afterwards is slow) and have limited outbound bandwidth. Check Render's current pricing page for the details.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `MAX_DOWNLOAD_MB` | `100` | Largest video the server will proxy |
| `MAX_CONCURRENT_DOWNLOADS` | `5` | Simultaneous downloads before returning "busy" |
| `PORT` | `8000` | Port to listen on (set by most hosts) |

Rate limits (per IP) are `LOOKUP_LIMIT` and `DOWNLOAD_LIMIT` at the top of `app.py`.

The Docker image trusts `X-Forwarded-For` from any source so rate limiting works behind a host's proxy. If you expose the container directly to the internet, change `--forwarded-allow-ips` to your proxy's IP.

## Keeping it working

X changes things often. If lookups start failing, update yt-dlp (`pip install -U yt-dlp`, or rebuild the Docker image).

## Responsible use

Only download content you have permission to use, and respect creators' rights. This project is not affiliated with or endorsed by X Corp.

## License

MIT. See [LICENSE](LICENSE).
