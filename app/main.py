import glob
import json
import os
import random
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi

# NOTE: authentication is intentionally disabled for testing. Re-add before production.
app = FastAPI(title="Clip Compute Plane")

MAX_CLIP_SECONDS = float(os.getenv("MAX_CLIP_SECONDS", "180"))
_render_slots = threading.Semaphore(int(os.getenv("RENDER_CONCURRENCY", "1")))
# Fewer encoder threads = much lower peak memory (helps on small containers)
FFMPEG_THREADS = os.getenv("FFMPEG_THREADS", "2")
# Preferred transcript language (ISO 639-1). Requests can override it with "lang".
TRANSCRIPT_LANG = os.getenv("TRANSCRIPT_LANG", "en").lower()

LEGACY_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/37.0.2062.94 Chrome/37.0.2062.94 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko",
    "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:40.0) Gecko/20100101 Firefox/40.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_5) AppleWebKit/600.8.9 (KHTML, like Gecko) Version/8.0.8 Safari/600.8.9",
    "Mozilla/5.0 (iPad; CPU OS 8_4_1 like Mac OS X) AppleWebKit/600.1.4 (KHTML, like Gecko) Version/8.0 Mobile/12H321 Safari/600.1.4",
    "Mozilla/5.0 (Windows NT 6.3; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.10240",
    "Mozilla/5.0 (Windows NT 6.3; WOW64; rv:40.0) Gecko/20100101 Firefox/40.0",
]

MODERN_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]


def pick_user_agent() -> str | None:
    # UA_MODE=off (default now: let yt-dlp use its own headers), legacy, or modern
    mode = os.getenv("UA_MODE", "off").lower()
    if mode == "legacy":
        return random.choice(LEGACY_USER_AGENTS)
    if mode == "modern":
        return random.choice(MODERN_USER_AGENTS)
    return None


def proxy_pool() -> list[str]:
    return [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]


def render_backend() -> str:
    # RENDER_BACKEND=rendi (hosted FFmpeg) or local (default here: FFmpeg inside this container)
    return os.getenv("RENDER_BACKEND", "local").lower()


# ---------- health ----------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "renderBackend": render_backend(),
        "transcriptProviders": os.getenv("TRANSCRIPT_PROVIDER", "native"),
        "transcriptLang": TRANSCRIPT_LANG,
        "proxies": len(proxy_pool()),
        "uaMode": os.getenv("UA_MODE", "off"),
        "cookies": bool(os.getenv("YOUTUBE_COOKIES")),
        "captions": True,
    }


# ---------- shared yt-dlp helpers ----------
def _short_error(stderr: str) -> str:
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    errs = [l for l in lines if l.startswith("ERROR")]
    msg = (errs[-1] if errs else (lines[-1] if lines else "no output"))
    return msg[:140]


def write_cookies(workdir: str) -> str | None:
    cookies = os.getenv("YOUTUBE_COOKIES", "")
    if not cookies:
        return None
    path = os.path.join(workdir, "cookies.txt")
    with open(path, "w") as f:
        f.write(cookies)
    return path


def ytdlp_attempts() -> list:
    """Proxies to try, in random order (up to 5). [None] = no proxy configured."""
    pool = proxy_pool()
    return random.sample(pool, k=min(5, len(pool))) if pool else [None]


def ytdlp_base(proxy: str | None, ua: str | None, cookies_path: str | None) -> list:
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if ua:
        cmd += ["--user-agent", ua]
    if proxy:
        cmd += ["--proxy", proxy]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    return cmd


# ---------- transcript ----------
class TranscriptIn(BaseModel):
    url: str
    # Preferred language (ISO 639-1). Defaults to TRANSCRIPT_LANG ("en").
    lang: str | None = None


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


def transcript_native(vid: str, lang: str) -> dict:
    """Direct YouTube caption fetch (often blocked from datacenter IPs)."""
    try:
        fetched = YouTubeTranscriptApi().fetch(vid, languages=[lang])
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
    return {"videoId": vid, "language": fetched.language_code, "availableLangs": [], "segments": segments}


def transcript_supadata(vid: str, lang: str) -> dict:
    """Supadata free tier. mode=native = existing captions only (1 credit).

    Always sends lang: without it Supadata returns the first available track, which
    can be a language other than the one spoken in the video.
    """
    key = os.getenv("SUPADATA_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="SUPADATA_API_KEY not set")
    q = urllib.parse.urlencode(
        {"url": f"https://www.youtube.com/watch?v={vid}", "mode": "native", "lang": lang}
    )
    req = urllib.request.Request(
        f"https://api.supadata.ai/v1/transcript?{q}", headers={"x-api-key": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        code = 422 if e.code in (400, 404) else 502
        raise HTTPException(status_code=code, detail=f"supadata {e.code}: {body}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"supadata request failed: {type(e).__name__}")
    content = data.get("content")
    if not isinstance(content, list):
        raise HTTPException(status_code=502, detail="supadata returned no transcript content")
    segments = []
    for c in content:
        text = " ".join(str(c.get("text", "")).split())
        if not text:
            continue
        start = float(c.get("offset", 0)) / 1000.0
        end = start + float(c.get("duration", 0)) / 1000.0
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return {
        "videoId": vid,
        "language": data.get("lang"),
        "availableLangs": data.get("availableLangs") or [],
        "segments": segments,
    }


def parse_json3(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segments = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        text = " ".join("".join(s.get("utf8", "") for s in segs).split())
        if not text:
            continue
        start = ev["tStartMs"] / 1000.0
        end = start + ev.get("dDurationMs", 0) / 1000.0
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return segments


def transcript_ytdlp(vid: str, lang: str) -> dict:
    """Captions via yt-dlp (no video download) through the proxy pool."""
    url = f"https://www.youtube.com/watch?v={vid}"
    errors = []
    with tempfile.TemporaryDirectory(prefix="subs-") as tmp:
        cookies_path = write_cookies(tmp)
        for i, proxy in enumerate(ytdlp_attempts(), 1):
            cmd = ytdlp_base(proxy, pick_user_agent(), cookies_path)
            cmd += ["--skip-download", "--write-subs", "--write-auto-subs",
                    "--sub-langs", f"{lang}.*,{lang}", "--sub-format", "json3",
                    "-o", os.path.join(tmp, "%(id)s.%(ext)s"), "--", url]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                errors.append(f"#{i} timed out")
                continue
            files = sorted(glob.glob(os.path.join(tmp, "*.json3")))
            if r.returncode == 0 and files:
                segments = parse_json3(files[0])
                if segments:
                    return {"videoId": vid, "language": lang, "availableLangs": [], "segments": segments}
                errors.append(f"#{i} empty captions")
                continue
            errors.append(f"#{i} {_short_error(r.stderr)}" if r.returncode else f"#{i} no '{lang}' captions found")
    raise HTTPException(
        status_code=422,
        detail="yt-dlp captions failed: " + " | ".join(errors),
    )


PROVIDERS = {
    "supadata": transcript_supadata,
    "ytdlp": transcript_ytdlp,
    "native": transcript_native,
}


@app.post("/transcript")
def transcript(body: TranscriptIn):
    vid = video_id_from_url(body.url)
    lang = (body.lang or TRANSCRIPT_LANG).strip().lower()
    # TRANSCRIPT_PROVIDER is an ordered, comma-separated fallback chain, e.g. "supadata,ytdlp"
    names = [n.strip().lower() for n in os.getenv("TRANSCRIPT_PROVIDER", "native").split(",") if n.strip()]
    errors = []
    for name in names:
        fn = PROVIDERS.get(name)
        if not fn:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            result = fn(vid, lang)
        except HTTPException as e:
            errors.append(f"{name}: {e.detail}")
            continue
        got = (result.get("language") or "").lower()
        result["requestedLanguage"] = lang
        # false when the video has no caption track in the requested language
        result["matchesRequested"] = got.startswith(lang)
        return result
    raise HTTPException(status_code=422, detail=" || ".join(errors) or "no transcript provider configured")


# ---------- captions (burned into the clip) ----------
class CaptionSeg(BaseModel):
    # Source-video seconds, the same values /transcript returns
    start: float
    end: float
    text: str


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _split_cue(text: str, limit: int = 40) -> list:
    chunks, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > limit:
            chunks.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(captions, clip_start: float, duration: float, w: int, h: int):
    """ASS subtitle text for the clip, or None when no caption falls inside it.

    Caption times are source-video seconds. They are shifted to clip time, long lines
    are split, overlaps are removed and exact repeats (rolling auto-captions) are merged.
    """
    cues = []
    for c in sorted(captions, key=lambda c: c.start):
        text = " ".join(c.text.replace("\n", " ").split())
        text = text.replace("{", "(").replace("}", ")").replace("\\", "")
        if not text or c.end <= clip_start or c.start >= clip_start + duration:
            continue
        s = max(0.0, c.start - clip_start)
        e = min(duration, c.end - clip_start)
        if e - s < 0.05:
            continue
        parts = _split_cue(text)
        total = sum(len(p) for p in parts) or 1
        t = s
        for p in parts:
            d = (e - s) * len(p) / total
            cues.append([t, t + d, p])
            t += d
    cleaned = []
    for i, (s, e, p) in enumerate(cues):
        if i + 1 < len(cues):
            e = min(e, cues[i + 1][0])
        if e - s < 0.05:
            continue
        if cleaned and cleaned[-1][2] == p and s - cleaned[-1][1] < 0.3:
            cleaned[-1][1] = e
            continue
        cleaned.append([s, e, p])
    if not cleaned:
        return None
    font = max(36, round(h * 0.034))
    outline = max(2, round(h * 0.0025))
    margin_v = round(h * 0.17)
    margin_lr = round(w * 0.08)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,DejaVu Sans,{font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_lr},{margin_lr},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    body = "\n".join(
        f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{p}" for s, e, p in cleaned
    )
    return head + body + "\n"


# ---------- render ----------
class RenderIn(BaseModel):
    sourceUrl: str
    start: float = Field(ge=0)
    end: float
    format: str = "1080x1920"
    # Optional: caption lines (source-video seconds). When given, they are burned into the clip.
    captions: list[CaptionSeg] | None = Field(default=None, max_length=3000)


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    return host == "youtu.be" or host.endswith("youtube.com")


def resolve_stream_urls(source_url: str, workdir: str):
    """Returns (urls, proxy, user_agent).

    Non-YouTube URLs (direct media files) are used as-is. YouTube pages are
    resolved to direct stream URL(s) via yt-dlp (nothing is downloaded). Those
    URLs are tied to the IP that resolved them, so a local FFmpeg must fetch
    through the same proxy (and user agent, if one is set).
    """
    if not is_youtube(source_url):
        return [source_url], None, None

    cookies_path = write_cookies(workdir)
    attempts = ytdlp_attempts()
    errors = []
    for i, proxy in enumerate(attempts, 1):
        ua = pick_user_agent()
        cmd = ytdlp_base(proxy, ua, cookies_path)
        cmd += ["-g", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--", source_url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            errors.append(f"#{i} timed out")
            continue
        urls = [l for l in r.stdout.splitlines() if l.startswith("http")]
        if r.returncode == 0 and urls:
            return urls[:2], proxy, ua
        errors.append(f"#{i} {_short_error(r.stderr)}")
    raise HTTPException(
        status_code=422,
        detail=f"could not resolve source stream ({len(attempts)} attempts): " + " | ".join(errors),
    )


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def ffmpeg_args(inputs: list, start: float, duration: float, vf: str, out_name: str, input_opts: list | None = None) -> list:
    """FFmpeg arguments (no binary name), shared by both backends.

    inputs / out_name are file paths, URLs or Rendi {{placeholders}}.
    input_opts are extra options placed before every -i (e.g. proxy, user agent).
    """
    args = []
    for name in inputs:
        args += list(input_opts or []) + ["-ss", str(start), "-i", name]
    args += ["-t", str(duration), "-map", "0:v:0", "-map", f"{len(inputs) - 1}:a:0?",
             "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-threads", FFMPEG_THREADS,
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out_name]
    return args


# ----- Rendi (hosted FFmpeg) -----
RENDI_BASE = "https://api.rendi.dev/v1"


def rendi_call(method: str, path: str, payload: dict | None = None) -> dict:
    key = os.getenv("RENDI_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="RENDI_API_KEY not set")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{RENDI_BASE}{path}", data=data, method=method,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "replace")
        raise HTTPException(status_code=502, detail=f"rendi {e.code}: {body}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"rendi request failed: {type(e).__name__}")


def render_with_rendi(streams: list, start: float, duration: float, vf: str, out_path: str) -> str:
    """Runs the trim/crop/scale on Rendi, downloads the result to out_path, returns the command id."""
    in_files = {f"in_{i + 1}": s for i, s in enumerate(streams)}
    placeholders = [f"{{{{in_{i + 1}}}}}" for i in range(len(streams))]
    args = ffmpeg_args(placeholders, start, duration, vf, "{{out_1}}")
    sub = rendi_call("POST", "/run-ffmpeg-command", {
        "input_files": in_files,
        "output_files": {"out_1": "clip.mp4"},
        "ffmpeg_command": shlex.join(args),
    })
    cid = sub.get("command_id")
    if not cid:
        raise HTTPException(status_code=502, detail=f"rendi gave no command_id: {str(sub)[:200]}")
    deadline = time.time() + float(os.getenv("RENDI_TIMEOUT", "240"))
    while time.time() < deadline:
        time.sleep(2)
        st = rendi_call("GET", f"/commands/{cid}")
        status = str(st.get("status", "")).upper()
        if status == "SUCCESS":
            url = ((st.get("output_files") or {}).get("out_1") or {}).get("storage_url")
            if not url:
                raise HTTPException(status_code=502, detail="rendi finished without an output URL")
            urllib.request.urlretrieve(url, out_path)
            return cid
        if status in ("FAILED", "ERROR"):
            raise HTTPException(status_code=502, detail=f"rendi command failed: {str(st)[:500]}")
    raise HTTPException(status_code=504, detail=f"rendi command {cid} still running after timeout")


@app.post("/render")
def render(body: RenderIn):
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", body.format)
    if not m:
        raise HTTPException(status_code=400, detail="format must be WxH, e.g. 1080x1920")
    w, h = int(m.group(1)), int(m.group(2))
    duration = body.end - body.start
    if duration <= 0 or duration > MAX_CLIP_SECONDS:
        raise HTTPException(status_code=400, detail=f"clip length must be 0-{MAX_CLIP_SECONDS:.0f}s")

    backend = render_backend()
    if body.captions and backend == "rendi":
        raise HTTPException(status_code=400, detail="captions need RENDER_BACKEND=local")

    vf = f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',scale={w}:{h},setsar=1"
    rendi_id = None
    caption_count = 0

    with _render_slots, tempfile.TemporaryDirectory(prefix="render-") as tmp:
        if body.captions:
            ass = build_ass(body.captions, body.start, duration, w, h)
            if ass:
                ass_path = os.path.join(tmp, "captions.ass")
                with open(ass_path, "w", encoding="utf-8") as f:
                    f.write(ass)
                caption_count = ass.count("\nDialogue:")
                vf += f",ass={ass_path}"

        streams, proxy, ua = resolve_stream_urls(body.sourceUrl, tmp)
        out = os.path.join(tmp, "clip.mp4")

        if backend == "rendi":
            rendi_id = render_with_rendi(streams, body.start, duration, vf, out)
        else:
            opts = []
            if ua:
                opts += ["-user_agent", ua]
            if proxy:
                opts += ["-http_proxy", proxy]
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            cmd += ffmpeg_args(streams, body.start, duration, vf, out, input_opts=opts)
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=504, detail="render timed out")
            if p.returncode != 0 or not os.path.exists(out):
                # negative return code = FFmpeg was killed by a signal (-9 usually means out of memory)
                raise HTTPException(
                    status_code=500,
                    detail=f"ffmpeg failed (exit code {p.returncode}): {p.stderr[-300:]}",
                )

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
        "backend": backend,
        "rendiCommandId": rendi_id,
        "captionsBurned": caption_count > 0,
        "captionCount": caption_count,
    }
