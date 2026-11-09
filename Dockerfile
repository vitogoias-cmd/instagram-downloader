# Backend: FastAPI + yt-dlp + ffmpeg (ffmpeg é necessário para mesclar áudio e vídeo)
FROM python:3.12-slim

# deno: runtime de JavaScript que o yt-dlp usa por padrão para extrair vídeos do YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static ./static

ENV PYTHONUNBUFFERED=1
# Render/Railway definem PORT automaticamente; o padrão aqui é 8000.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
