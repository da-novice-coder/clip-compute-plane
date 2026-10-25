FROM python:3.12-slim
# fonts-dejavu-core: caption font (covers Latin and Arabic) used by FFmpeg's subtitle renderer
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
# Deno = JS runtime yt-dlp needs to solve YouTube's player challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONPATH=/srv PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
