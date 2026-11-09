"""
Instagram Downloader - backend FastAPI + yt-dlp.

Rotas:
  GET  /                  -> interface web (static/index.html)
  POST /api/info          -> extrai metadados e URLs diretas de vídeo (e áudio, quando separado)
  GET  /api/download      -> repassa o MP4 com Content-Disposition: attachment.
                             Com o parâmetro `audio`, baixa vídeo e áudio separados e os mescla com ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from yt_dlp.utils import DownloadError, ExtractorError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

INSTAGRAM_DOMAIN = "instagram.com"
# Hosts de CDN do Instagram/Meta aceitos pelo proxy (evita uso como open proxy / SSRF).
YOUTUBE_DOMAINS = ("youtube.com", "youtu.be")
# CDNs aceitos pelo proxy (evita uso como open proxy / SSRF): Instagram/Meta e vídeos do YouTube (googlevideo).
ALLOWED_CDN_SUFFIXES = ("cdninstagram.com", "fbcdn.net", "instagram.com", "googlevideo.com")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _cdn_headers(url: str) -> dict[str, str]:
    host = (urlparse(url).hostname or "").lower()
    referer = "https://www.youtube.com/" if host.endswith("googlevideo.com") else "https://www.instagram.com/"
    return {"User-Agent": USER_AGENT, "Referer": referer}

logger = logging.getLogger("instagram-downloader")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Instagram Video Downloader", version="1.1.0")

# Origens permitidas para o frontend. Em produção, defina ALLOWED_ORIGINS com o domínio do Netlify,
# separado por vírgula (ex.: https://meu-app.netlify.app). Sem a variável, libera tudo (dev local).
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
class InfoRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=2048, examples=["https://www.instagram.com/reel/XXXX/"])


class InfoResponse(BaseModel):
    title: str
    thumbnail: Optional[str]
    download_url: str
    audio_url: Optional[str] = None
    duration: Optional[float]
    has_audio: bool = True


# ---------------------------------------------------------------------------
# Helpers de validação
# ---------------------------------------------------------------------------
def _is_instagram_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host == INSTAGRAM_DOMAIN or host.endswith("." + INSTAGRAM_DOMAIN)


def _is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in YOUTUBE_DOMAINS)


def _is_allowed_cdn_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in ALLOWED_CDN_SUFFIXES)


def _find_ffmpeg() -> Optional[str]:
    """ffmpeg via variável FFMPEG_PATH ou PATH."""
    custom = os.environ.get("FFMPEG_PATH")
    if custom and Path(custom).is_file():
        return custom
    return shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# Extração com yt-dlp (bloqueante -> executado em thread)
# ---------------------------------------------------------------------------
def _extract_info_sync(url: str) -> dict[str, Any]:
    ydl_opts = {
        "skip_download": True,      # inspeciona metadados sem gravar no disco
        "quiet": True,
        "no_warnings": True,
        # YouTube: só o vídeo da URL (ignora playlist). Instagram: carrosséis chegam como playlist.
        "noplaylist": _is_youtube_url(url),
        "logger": logging.getLogger("yt-dlp"),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ExtractorError("Nenhuma informação retornada para esta URL.")
    return info


def _pick_media_entry(info: dict[str, Any]) -> dict[str, Any]:
    """Para carrosséis, retorna o primeiro item que contém vídeo."""
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        for entry in info.get("entries") or []:
            if not entry:
                continue
            if "entries" in entry:  # aninhamento raro
                try:
                    return _pick_media_entry(entry)
                except HTTPException:
                    continue
            if _has_video(entry):
                return entry
        raise HTTPException(status_code=404, detail="Nenhum vídeo encontrado neste post (o carrossel pode conter apenas fotos).")
    if not _has_video(info):
        raise HTTPException(status_code=404, detail="Nenhum vídeo encontrado neste post.")
    return info


def _has_video(entry: dict[str, Any]) -> bool:
    if entry.get("vcodec") not in (None, "none"):
        return True
    if entry.get("ext") == "mp4" and entry.get("url"):
        return True
    return any(f.get("vcodec") not in (None, "none") for f in entry.get("formats") or [])


def _select_streams(entry: dict[str, Any]) -> tuple[str, Optional[str], bool]:
    """
    Escolhe as URLs necessárias para o vídeo com som.

    Retorna (video_url, audio_url, tem_audio):
      - Se existe um formato com vídeo e áudio juntos, usa só ele (audio_url = None).
      - Senão, usa o melhor vídeo puro e o melhor áudio puro separados (audio_url preenchida).
        A mesclagem acontece no servidor, com ffmpeg.
      - Se não há áudio em lugar nenhum, tem_audio = False.
    """
    # Só formatos HTTP(S) diretos: HLS (m3u8) e DASH entregam playlists/fragmentos, não o arquivo.
    formats = [
        f for f in (entry.get("formats") or [])
        if f.get("url") and (f.get("protocol") or "").lower() in ("http", "https")
    ]
    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none")]
    audio_formats = [f for f in formats if f.get("acodec") not in (None, "none")
                     and f.get("vcodec") in (None, "none")]

    # 1) Formato muxed (vídeo + áudio no mesmo arquivo).
    #    acodec None = desconhecido, pode ter som; acodec "none" = sem som.
    muxed = [f for f in video_formats if f.get("acodec") != "none"]
    if muxed:
        best = max(muxed, key=_video_score)
        return best["url"], None, True

    if not video_formats:
        if entry.get("url"):
            return entry["url"], None, True  # sem lista de formatos: não dá para afirmar a ausência de som
        raise HTTPException(status_code=404, detail="Não foi possível obter uma URL de download para este vídeo.")

    # 2) Vídeo puro + áudio separado.
    best_video = max(video_formats, key=_video_score)
    if audio_formats:
        best_audio = max(audio_formats, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))
        return best_video["url"], best_audio["url"], True

    # 3) Só vídeo puro existe.
    logger.warning("Nenhuma trilha de áudio encontrada para este post.")
    return best_video["url"], None, False


def _video_score(f: dict[str, Any]) -> tuple:
    return (f.get("ext") == "mp4", f.get("height") or 0, f.get("tbr") or 0)


def _map_ytdlp_error(exc: Exception) -> HTTPException:
    msg = str(exc)
    lowered = msg.lower()

    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return HTTPException(
            status_code=429,
            detail="O YouTube pediu verificação anti-bot para este servidor. Tente novamente mais tarde.",
        )
    if any(k in lowered for k in ("rate-limit", "rate limit", "too many requests", "429")):
        return HTTPException(
            status_code=429,
            detail="O Instagram limitou as requisições (rate limit). Aguarde alguns minutos e tente novamente.",
        )
    if any(k in lowered for k in ("private", "login required", "not available", "unavailable",
                                  "does not exist", "no video", "not found", "404", "removed",
                                  "empty media response", "cookies", "requested content")):
        return HTTPException(
            status_code=404,
            detail="Conteúdo não encontrado ou privado. Verifique se o link está correto e se o perfil é público.",
        )
    logger.exception("Erro não tratado do yt-dlp")
    return HTTPException(status_code=500, detail=f"Falha ao processar o link no Instagram: {msg[:300]}")


# ---------------------------------------------------------------------------
# Download e mesclagem
# ---------------------------------------------------------------------------
async def _save_stream(client: httpx.AsyncClient, url: str, path: Path) -> None:
    async with client.stream("GET", url, headers=_cdn_headers(url)) as resp:
        if resp.status_code >= 400:
            raise HTTPException(status_code=404, detail="O link de mídia expirou. Analise o post novamente.")
        with path.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                fh.write(chunk)


async def _download_merged(video_url: str, audio_url: str) -> FileResponse:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg não encontrado. Instale o ffmpeg e adicione-o ao PATH (ou defina FFMPEG_PATH) para baixar vídeos com som.",
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="igdl_"))
    cleanup = BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True)
    video_path = tmpdir / "video.mp4"
    audio_path = tmpdir / "audio.m4a"
    out_path = tmpdir / "instagram_video.mp4"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True) as client:
            await _save_stream(client, video_url, video_path)
            await _save_stream(client, audio_url, audio_path)

        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy", "-movflags", "+faststart",
            str(out_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not out_path.exists():
            logger.error("ffmpeg falhou: %s", stderr.decode(errors="replace")[:500])
            raise HTTPException(status_code=500, detail="Falha ao mesclar vídeo e áudio com o ffmpeg.")
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except httpx.HTTPError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.warning("Erro ao contatar o CDN: %s", exc)
        raise HTTPException(status_code=502, detail="Não foi possível baixar o arquivo do CDN do Instagram.") from exc

    return FileResponse(
        out_path,
        media_type="video/mp4",
        filename="instagram_video.mp4",
        background=cleanup,
    )


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/info", response_model=InfoResponse)
async def get_info(payload: InfoRequest) -> InfoResponse:
    url = payload.url.strip()
    if not (_is_instagram_url(url) or _is_youtube_url(url)):
        raise HTTPException(status_code=400, detail="URL inválida. Informe um link do Instagram ou do YouTube.")

    try:
        info = await asyncio.to_thread(_extract_info_sync, url)
    except HTTPException:
        raise
    except (DownloadError, ExtractorError) as exc:
        raise _map_ytdlp_error(exc) from exc
    except Exception as exc:  # erros inesperados
        logger.exception("Falha inesperada ao extrair metadados")
        raise HTTPException(status_code=500, detail="Erro interno ao processar a requisição.") from exc

    entry = _pick_media_entry(info)
    video_url, audio_url, has_audio = _select_streams(entry)

    # Sem ffmpeg não há como mesclar: baixa o vídeo e avisa que não há som.
    if audio_url and not _find_ffmpeg():
        logger.warning("ffmpeg ausente: o vídeo sairá sem áudio.")
        audio_url, has_audio = None, False

    return InfoResponse(
        title=(entry.get("title") or info.get("title") or "Vídeo do Instagram").strip(),
        thumbnail=entry.get("thumbnail") or info.get("thumbnail"),
        download_url=video_url,
        audio_url=audio_url,
        duration=entry.get("duration") or info.get("duration"),
        has_audio=has_audio,
    )


@app.get("/api/download")
async def download(
    url: str = Query(..., min_length=10, max_length=4096),
    audio: Optional[str] = Query(None, min_length=10, max_length=4096),
):
    if not _is_allowed_cdn_url(url) or (audio and not _is_allowed_cdn_url(audio)):
        raise HTTPException(status_code=400, detail="URL de mídia inválida para o proxy de download.")

    if audio:
        return await _download_merged(url, audio)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=60.0),
        follow_redirects=True,
    )
    try:
        request = client.build_request("GET", url, headers=_cdn_headers(url))
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("Erro ao contatar o CDN: %s", exc)
        raise HTTPException(status_code=502, detail="Não foi possível baixar o arquivo do CDN do Instagram.") from exc

    if upstream.status_code >= 400:
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        detail = "O link de mídia expirou. Analise o post novamente."
        raise HTTPException(status_code=404 if status in (403, 404, 410) else 502, detail=detail)

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": 'attachment; filename="instagram_video.mp4"',
        "Cache-Control": "no-store",
    }
    if upstream.headers.get("content-length"):
        headers["Content-Length"] = upstream.headers["content-length"]

    return StreamingResponse(stream_body(), media_type="video/mp4", headers=headers)


if __name__ == "__main__":
    import uvicorn

    # PORT é definido por serviços como Render/Railway; localmente usa 8000.
    uvicorn.run("main:app", host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
                port=int(os.environ.get("PORT", "8000")), reload=False)
