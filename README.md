# Instagram Video Downloader

Aplicação web para baixar vídeos de **Reels** e **posts com vídeo** do Instagram, e de **vídeos do YouTube**.
Backend em **FastAPI** usando **yt-dlp** para extrair os metadados; frontend em HTML/CSS/JS puro, servido pelo próprio FastAPI.

## Estrutura

```
.
├── main.py            # API FastAPI, extração com yt-dlp e proxy de download
├── requirements.txt   # Dependências
├── README.md
└── static/
    └── index.html     # Interface web (responsiva)
```

## Requisitos

- Python 3.9 ou superior
- **ffmpeg** no PATH (ou apontado pela variável `FFMPEG_PATH`). O Instagram envia o áudio dos Reels em um stream separado do vídeo, e o ffmpeg é necessário para juntar os dois. Sem ele, o vídeo sai sem som.
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux (Debian/Ubuntu): `sudo apt install ffmpeg`
- Conexão com a internet

## Instalação

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (cmd)
.venv\Scripts\activate.bat
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## Execução

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Depois abra [http://127.0.0.1:8000](http://127.0.0.1:8000) no navegador, cole o link do Reel ou post e clique em **Buscar**.

Para expor na rede local, use `--host 0.0.0.0`.

## Endpoints

| Método | Rota | Descrição |
|-------|------|-----------|
| GET | `/` | Interface web |
| POST | `/api/info` | Body `{"url": "https://www.instagram.com/..."}`. Retorna `title`, `thumbnail`, `download_url`, `duration`. |
| GET | `/api/download?url=...` | Proxy de streaming do arquivo MP4 com `Content-Disposition: attachment; filename="instagram_video.mp4"`. |
| GET | `/docs` | Documentação interativa (Swagger) |

### Códigos de erro

| Status | Quando |
|-------|--------|
| 400 | URL vazia/inválida ou que não pertence a `instagram.com` |
| 404 | Post privado, removido, inexistente, ou sem vídeo (ex.: carrossel só com fotos) |
| 429 | O Instagram aplicou rate limit |
| 500 | Falha inesperada na extração |
| 502 | Falha ao buscar o arquivo no CDN durante o download |

## Como funciona

1. `POST /api/info` roda o `yt_dlp.YoutubeDL` com `skip_download=True` (nada é gravado em disco).
2. Para carrosséis, é usado o primeiro item que contém vídeo.
3. Entre os formatos disponíveis, é escolhido o de maior resolução, preferindo arquivos que já trazem áudio e vídeo juntos.
4. O navegador chama `/api/download`, que repassa os bytes do CDN do Instagram em streaming, forçando o salvamento do arquivo.

O proxy só aceita URLs HTTPS de domínios do Instagram/Meta (`cdninstagram.com`, `fbcdn.net`, `instagram.com`), para evitar que o servidor seja usado como proxy aberto.

## Notas sobre rate limit e limitações

- **Rate limit:** o Instagram limita requisições anônimas com bastante agressividade. Se aparecer a mensagem de rate limit (HTTP 429), aguarde alguns minutos. Evite fazer muitas buscas seguidas.
- **Conteúdo privado:** posts de perfis privados exigem login e não são suportados por esta aplicação. Retornam 404.
- **Links expiram:** as URLs do CDN são assinadas e expiram. Se o download falhar depois de um tempo, busque o link novamente.
- **Manter o yt-dlp atualizado:** o Instagram muda a estrutura com frequência. Quando a extração começar a falhar sem motivo aparente, atualize a dependência:
  ```bash
  pip install -U yt-dlp
  ```
- **Autenticação (opcional):** para casos específicos, o yt-dlp aceita cookies de sessão (`cookiefile`/`cookiesfrombrowser`) em `_extract_info_sync` no `main.py`. Use apenas com a sua própria conta e sob as regras do Instagram.
- **Uso responsável:** baixe apenas conteúdo que você tem direito de usar. Os Termos de Uso do Instagram e os direitos autorais dos criadores continuam valendo.

## YouTube

- Aceita links `youtube.com/watch?v=...` e `youtu.be/...`. Playlists são ignoradas: só o vídeo da URL é baixado.
- Vídeos até 720p costumam vir com áudio junto. Resoluções maiores vêm com áudio separado, e o ffmpeg faz a mesclagem.
- O YouTube bloqueia com frequência IPs de datacenter e pode pedir verificação anti-bot (HTTP 429). Nesse caso, tente de novo mais tarde ou rode o app em outra máquina.
- Baixar vídeos do YouTube sem autorização viola os Termos de Serviço da plataforma. Use apenas para vídeos que você tem direito de baixar, como os seus próprios ou os de licença livre.

## Produção

`allow_origins=["*"]` no CORS está liberado para desenvolvimento local. Em produção, restrinja aos domínios do seu frontend e coloque a aplicação atrás de um proxy reverso (Nginx, Caddy) com HTTPS.
