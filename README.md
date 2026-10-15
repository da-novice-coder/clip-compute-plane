# Clip Compute Plane
Transcript extraction + FFmpeg rendering only. Auth is `Authorization: Bearer $INTERNAL_SERVICE_TOKEN`.

- `POST /transcript` `{ "url": "<youtube url>" }` -> `{ videoId, language, segments:[{start,end,text}] }`
- `POST /render` `{ "sourceUrl", "start", "end", "format": "1080x1920" }` -> `{ clipUrl, key, bytes, durationSec, ... }`
- `GET /health`

Source video is streamed straight into FFmpeg (never stored); the temp clip is deleted after R2 upload.
Env vars: see `.env.example`.
