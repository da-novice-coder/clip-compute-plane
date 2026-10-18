# Clip Compute Plane

A small, dedicated service that does the two heavy jobs in the clip pipeline:

1. **Transcript extraction** - YouTube URL in, timed captions out.
2. **FFmpeg rendering** - source video + time range in, vertical MP4 clip out (uploaded to Cloudflare R2).

Everything else (auth, database, LLM scoring, orchestration, scheduling, clip bank, publishing) lives upstream in **Supabase + Lovable**. This service is stateless: it returns results and never writes metadata itself.

```
YouTube URL -> /transcript -> LLM scoring -> /render (per clip) -> R2 -> Supabase clip bank -> publish
```

## Status

- `/health`, `/transcript` and `/render` are deployed on Railway and tested.
- `/render` has been verified end to end on a YouTube URL (through Oxylabs ISP proxies) and on a direct MP4 link.
- **Authentication is currently disabled for testing.** Anyone with the URL can call the API. Re-add it before real use (see [Security](#security)).

## Endpoints

Interactive test page: `GET /docs` (Swagger UI). It works from a phone browser - no extra tools needed.

### `GET /health`

Liveness check (used by Railway health checks). Also reports the active configuration:

```json
{
  "status": "ok",
  "renderBackend": "local",
  "transcriptProviders": "supadata,ytdlp",
  "proxies": 3,
  "uaMode": "off",
  "cookies": false
}
```

### `POST /transcript`

Request:

```json
{ "url": "https://www.youtube.com/watch?v=VIDEO_ID" }
```

Response (times in seconds):

```json
{
  "videoId": "VIDEO_ID",
  "language": "en",
  "segments": [{ "start": 0.0, "end": 2.4, "text": "..." }]
}
```

Accepted URL forms: `watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`.

Providers are tried in the order set by `TRANSCRIPT_PROVIDER` (comma-separated). The first one that succeeds wins; if all fail, the error lists what each provider said.

| Provider | What it does |
|----------|--------------|
| `supadata` | Supadata API, captions only (`mode=native`, about 1 credit per video). Free tier is limited (roughly 100 credits per month). |
| `ytdlp` | Downloads captions only (no video) with `yt-dlp` through the proxy pool. Auto-generated captions can contain overlapping lines. |
| `native` | `youtube-transcript-api` directly from the server. Usually blocked from cloud IPs. |

### `POST /render`

Request:

```json
{
  "sourceUrl": "https://www.youtube.com/watch?v=VIDEO_ID",
  "start": 10,
  "end": 25,
  "format": "1080x1920"
}
```

- `sourceUrl`: a YouTube URL **or** a direct link to a video file (MP4 etc.).
- `start` / `end`: seconds. The clip length must be greater than 0 and at most `MAX_CLIP_SECONDS` (default 180).
- `format`: `WIDTHxHEIGHT`. The video is center-cropped to that aspect ratio and scaled to that size.

Response:

```json
{
  "clipUrl": "https://pub-....r2.dev/clips/<id>.mp4",
  "key": "clips/<id>.mp4",
  "bytes": 1002091,
  "durationSec": 3,
  "format": "1080x1920",
  "start": 1,
  "end": 4,
  "backend": "local",
  "rendiCommandId": null
}
```

How it works:

1. For YouTube URLs, `yt-dlp` resolves direct stream URLs through a random proxy (up to 5 attempts). Nothing is downloaded. Direct file links skip this step.
2. FFmpeg reads only the requested range straight from those URLs (through the same proxy) and writes an H.264/AAC MP4 with `faststart`.
3. The clip is uploaded to R2 and the temp file is deleted. The source video is never stored.

Errors: `400` bad input, `422` could not resolve the source (the message lists each attempt), `500` FFmpeg failed (the message includes the exit code; `-9` means out of memory), `502`/`504` hosted-FFmpeg problems.

## Configuration (Railway environment variables)

Copy `.env.example` for the full list. Never commit real values.

| Variable | Required | Purpose |
|----------|----------|---------|
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | yes | Cloudflare R2 upload (S3-compatible API) |
| `R2_PUBLIC_BASE_URL` | recommended | Public `r2.dev` or custom-domain base for permanent clip links. If unset, 7-day presigned URLs are returned. |
| `TRANSCRIPT_PROVIDER` | yes | Ordered provider chain, e.g. `supadata,ytdlp` |
| `SUPADATA_API_KEY` | if using `supadata` | Supadata API key |
| `PROXY_LIST` | for YouTube | Comma-separated proxy URLs, e.g. `http://user:pass@host:port,...` Used by `yt-dlp` and FFmpeg. Datacenter proxies are usually blocked by YouTube; ISP/residential ones work better. |
| `UA_MODE` | no | `off` (default, recommended - `yt-dlp` uses its own headers), `legacy` or `modern` to force a random user agent |
| `YOUTUBE_COOKIES` | no | Contents of a Netscape `cookies.txt`, an extra option if YouTube still blocks requests |
| `RENDER_BACKEND` | no | `local` (default, FFmpeg in this container) or `rendi` (hosted FFmpeg) |
| `RENDI_API_KEY`, `RENDI_TIMEOUT` | only for `rendi` | Hosted FFmpeg key and wait limit (seconds) |
| `FFMPEG_THREADS` | no | Encoder threads (default 2). Fewer threads means lower peak memory. |
| `RENDER_CONCURRENCY` | no | Parallel renders per container (default 1) |
| `MAX_CLIP_SECONDS` | no | Longest allowed clip (default 180) |
| `INTERNAL_SERVICE_TOKEN` | not used right now | Reserved for when auth is re-enabled |

## Deployment

- Railway builds the `Dockerfile` (Python 3.12, FFmpeg, and Deno, which `yt-dlp` needs to solve YouTube's player challenges).
- Deploys automatically on every push to `main`. `railway.json` points the health check at `/health`.
- Changing a variable in Railway redeploys the service.

## Editing workflow (Drive -> GitHub sync)

The project is maintained from a Google Drive folder named `clip-compute-plane` (same relative paths as this repo). A Make.com scenario syncs Drive to this repository **one way**: new or changed files are created or updated on `main` with their paths preserved, which triggers a Railway deploy.

- Edit files in **Drive**. Changes made directly on GitHub are not copied back and can be overwritten.
- The scenario runs every 15 minutes and only picks up files modified in the last 20 minutes.
- If you save two files at nearly the same moment, GitHub can reject one with a 409 conflict. Run the scenario again to retry.
- File deletions in Drive are not mirrored to GitHub.

## Testing from a phone

- Open `https://<your-railway-domain>/docs`, choose an endpoint, tap **Try it out**, then **Execute**.
- Safe first `/render` test with a direct link (no YouTube involved):
  `{"sourceUrl":"https://storage.rendi.dev/sample/big_buck_bunny_720p_5sec_intro.mp4","start":1,"end":4,"format":"1080x1920"}`

## Known limitations

- **YouTube blocks cloud IPs.** Without good proxies, `yt-dlp` fails with "Sign in to confirm you're not a bot". If ISP proxies stop working, options are cookies from a throwaway account, residential proxies, or uploading the source video yourself and passing its direct link as `sourceUrl`.
- Proxy-assigned IPs can be changed by the provider; keep `PROXY_LIST` current.
- `/render` only processes one clip at a time per container and is memory-hungry at 1080x1920. If FFmpeg exits with code `-9`, give the Railway service more memory or lower `FFMPEG_THREADS`.
- Downloading YouTube content may conflict with YouTube's Terms of Service and with the video owners' copyright. Only process content you have the right to use.

## Security

- Authentication was removed for testing. Before real use, restore a shared-secret check (for example an `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>` header validated on `/transcript` and `/render`) so only Supabase/Lovable can call the service.
- Keep all keys in Railway variables, never in the code or this repository.
- Test credentials (R2, proxies, Supadata, Rendi, GitHub) should be rotated or deleted when development ends.

## Project layout

```
Dockerfile           Python 3.12 + FFmpeg + Deno
railway.json         Railway build and health-check config
requirements.txt     Python dependencies
.env.example         Environment variable template
app/main.py          FastAPI app: /health, /transcript, /render
```
