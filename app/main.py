import hmac
import os
import re
import subprocess
import tempfile
import threading
import uuid
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi

app = FastAPI(title="Clip Compute Plane")

MAX_CLIP_SECONDS = float(os.getenv("MAX_CLIP_SECONDS", "180"))
_render_slots = threading.Semaphore(int(os.getenv("RENDER_CONCURRENCY", "1")))


# ---------- auth ----------
def require_token(authorization: str = Header(default="")):
    expected = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------- health ----------
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- transcript ----------
class TranscriptIn(BaseModel):
    url: str


def video_id_from_url(url: str) -> str:
    u = urlparse(url)
    host = (u.hostname or "").removeprefix("www.")
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if u.path == "/watch":
            vid = parse_qs(u.query).get("v", [""])[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v)/([\w-]{11})", u.path)
            vid = m.group(1) if m else ""
    else:
        vid = ""
    if not re.fullmatch(r"[\w-]{11}", vid):
        raise HTTPException(status_code=400, detail="not a valid YouTube URL")
    return vid


@app.post("/transcript", dependencies=[Depends(require_token)])
def transcript(body: TranscriptIn):
    vid = video_id_from_url(body.url)
    try:
        fetched = YouTubeTranscriptApi().fetch(vid, languages=["en"])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"captions unavailable: {type(e).__name__}")
    segments = [
        {
            "start": round(s.start, 3),
            "end": round(s.start + s.duration, 3),
            "text": " ".join(s.text.split()),
        }
        for s in fetched
        if s.text.strip()
    ]
    return {"videoId": vid, "language": fetched.language_code, "segments": segments}


# ---------- render ----------
class RenderIn(BaseModel):
    sourceUrl: str
    start: float = Field(ge=0)
    end: float
    format: str = "1080x1920"


def resolve_stream_urls(source_url: str) -> list[str]:
    """YouTube pages -> direct stream URL(s) via yt-dlp (nothing downloaded)."""
    host = (urlparse(source_url).hostname or "").removeprefix("www.")
    if not (host == "youtu.be" or host.endswith("youtube.com")):
        return [source_url]
    r = subprocess.run(
        ["yt-dlp", "-g", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--no-playlist", source_url],
        capture_output=True, text=True, timeout=60,
    )
    urls = [l for l in r.stdout.splitlines() if l.startswith("http")]
    if r.returncode != 0 or not urls:
        raise HTTPException(status_code=422, detail="could not resolve source stream")
    return urls[:2]


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


@app.post("/render", dependencies=[Depends(require_token)])
def render(body: RenderIn):
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", body.format)
    if not m:
        raise HTTPException(status_code=400, detail="format must be WxH, e.g. 1080x1920")
    w, h = int(m.group(1)), int(m.group(2))
    duration = body.end - body.start
    if duration <= 0 or duration > MAX_CLIP_SECONDS:
        raise HTTPException(status_code=400, detail=f"clip length must be 0-{MAX_CLIP_SECONDS:.0f}s")

    streams = resolve_stream_urls(body.sourceUrl)
    vf = f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',scale={w}:{h},setsar=1"

    with _render_slots, tempfile.TemporaryDirectory(prefix="render-") as tmp:
        out = os.path.join(tmp, "clip.mp4")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for s in streams:
            cmd += ["-ss", str(body.start), "-i", s]
        cmd += ["-t", str(duration), "-map", "0:v:0", "-map", f"{len(streams) - 1}:a:0?",
                "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="render timed out")
        if p.returncode != 0 or not os.path.exists(out):
            raise HTTPException(status_code=500, detail=f"ffmpeg failed: {p.stderr[-300:]}")

        size = os.path.getsize(out)
        key = f"clips/{uuid.uuid4().hex}.mp4"
        bucket = os.environ["R2_BUCKET"]
        c = r2_client()
        c.upload_file(out, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        # temp dir (and the clip file) is removed when this block exits

    public = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{public}/{key}" if public else c.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=7 * 24 * 3600)
    return {
        "clipUrl": url,
        "key": key,
        "bytes": size,
        "durationSec": round(duration, 3),
        "format": body.format,
        "start": body.start,
        "end": body.end,
    }
