import asyncio
from contextlib import asynccontextmanager
import logging
import secrets
import time
from typing import Optional
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .models import LinkCreateRequest, LinkCreateResponse, LinkInfoResponse
from .storage import LinkStore

logger = logging.getLogger("uvicorn.error")

store = LinkStore(settings.db_path)
security = HTTPBearer(auto_error=False)


async def _periodic_cleanup_loop(interval_seconds: int) -> None:
    # Breve pausa inicial para permitir que el servidor arranque completamente
    try:
        await asyncio.sleep(min(5, interval_seconds))
        now = int(time.time())
        deleted = await asyncio.to_thread(store.cleanup_expired, now)
        if deleted > 0:
            logger.info(f"[AutoCleanup] Limpieza inicial: {deleted} enlace(s) expirados/agotados eliminados.")
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.error(f"[AutoCleanup] Error en limpieza inicial: {exc}")

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            now = int(time.time())
            deleted = await asyncio.to_thread(store.cleanup_expired, now)
            if deleted > 0:
                logger.info(f"[AutoCleanup] Limpieza periódica: {deleted} enlace(s) expirados/agotados eliminados.")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"[AutoCleanup] Error durante la limpieza automática: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = None
    if settings.auto_cleanup_enabled:
        cleanup_task = asyncio.create_task(_periodic_cleanup_loop(settings.cleanup_interval_seconds))
        logger.info(
            f"[AutoCleanup] Tarea de limpieza automática iniciada (intervalo: {settings.cleanup_interval_seconds}s)"
        )
    yield
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Configuración de CORS
origins = settings.cors_origins_list
allow_all = "*" in origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=settings.cors_allow_credentials and not allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_api_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    request: Request = None,
) -> None:
    token = ""
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif request:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        else:
            token = auth_header.strip()

    if not token or not secrets.compare_digest(token, settings.api_token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_public_url(slug: str) -> str:
    base = str(settings.base_url).rstrip("/")
    return f"{base}/s/{slug}"


def _sanitize_filename(name: str, fallback: str = "file") -> tuple[str, str]:
    """
    Sanea un nombre de archivo para usar en Content-Disposition.
    Retorna (ascii_filename, utf8_encoded_filename).
    Elimina caracteres de control, saltos de línea (prevención CRLF) y comillas.
    """
    if not name:
        name = fallback

    clean = name.replace("\\", "/").split("/")[-1].strip()
    clean = "".join(ch for ch in clean if ord(ch) >= 32 and ord(ch) != 127)
    clean = clean.replace('"', "").replace(";", "_").strip()

    if not clean:
        clean = fallback

    # Versión ASCII transliterando acentos/ñ para máxima compatibilidad
    ascii_clean = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii").strip()
    ext = clean.rsplit(".", 1)[1] if "." in clean else ""
    stem = ascii_clean.rsplit(".", 1)[0] if "." in ascii_clean else ascii_clean
    if not stem.strip():
        ascii_clean = f"{fallback}.{ext}" if ext else fallback

    # Versión UTF-8 codificada según RFC 5987 / RFC 6266
    utf8_encoded = quote(clean, safe="!#$&+-.^_`|~")
    return ascii_clean, utf8_encoded


def _build_content_disposition(filename: Optional[str], target_url: str, slug: str) -> str:
    candidate = filename
    if not candidate:
        parsed = urlsplit(target_url)
        path = unquote(parsed.path or "")
        base = path.rstrip("/").split("/")[-1].strip() if path else ""
        if base:
            candidate = base

    ascii_name, utf8_name = _sanitize_filename(candidate, fallback=slug)
    return f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


def _build_internal_redirect(target_url: str, passthrough_query: str) -> str:
    parsed = urlsplit(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="target_url inválido")

    raw_path = parsed.path or "/"
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"

    # Conservar el query string crudo sin decodificar ni re-codificar
    # para evitar invalidar firmas en URLs de S3, Azure Blob, Google Cloud Storage, etc.
    target_query = parsed.query or ""
    extra_query = passthrough_query or ""
    if target_query and extra_query:
        merged_query = f"{target_query}&{extra_query}"
    elif target_query:
        merged_query = target_query
    elif extra_query:
        merged_query = extra_query
    else:
        merged_query = ""

    base = f"/_internal_proxy/{parsed.scheme}/{parsed.netloc}{raw_path}"
    return f"{base}?{merged_query}" if merged_query else base



@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/links", response_model=LinkCreateResponse, dependencies=[Depends(_require_api_token)])
def create_link(req: LinkCreateRequest) -> LinkCreateResponse:
    if req.expires_in == 0:
        expires_at = 0
    else:
        raw_expires = req.expires_in if req.expires_in is not None else settings.default_expires_in
        expires_in = min(raw_expires, settings.max_expires_in)
        expires_at = int(time.time()) + expires_in

    slug = req.custom_slug or secrets.token_urlsafe(8)
    if len(slug) > settings.max_slug_len:
        raise HTTPException(status_code=400, detail="Slug demasiado largo")

    filename = req.filename
    # Si no se pasó filename explícito pero el custom_slug tiene extensión (ej. Video.mp4),
    # usarlo como nombre de descarga
    if not filename and req.custom_slug and "." in req.custom_slug:
        filename = req.custom_slug

    try:
        store.create_link(
            slug=slug,
            target_url=str(req.target_url),
            expires_at=expires_at,
            filename=filename,
            max_uses=req.max_uses,
        )
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="Slug en uso") from exc
        raise

    return LinkCreateResponse(slug=slug, url=_build_public_url(slug), expires_at=expires_at)


@app.get("/api/links/{slug}", response_model=LinkInfoResponse, dependencies=[Depends(_require_api_token)])
def get_link(slug: str) -> LinkInfoResponse:
    rec = store.get_link(slug)
    if not rec:
        raise HTTPException(status_code=404, detail="No existe")
    return LinkInfoResponse(**rec.__dict__)


@app.delete("/api/links/{slug}", dependencies=[Depends(_require_api_token)])
def revoke_link(slug: str) -> dict:
    ok = store.revoke_link(slug)
    if not ok:
        raise HTTPException(status_code=404, detail="No existe")
    return {"revoked": True, "slug": slug}


@app.post("/api/cleanup", dependencies=[Depends(_require_api_token)])
def cleanup_links() -> dict:
    deleted = store.cleanup_expired(int(time.time()))
    return {"deleted": deleted}


@app.get("/internal-resolve/{slug}")
def resolve_link(slug: str, request: Request, response: Response):
    now = int(time.time())
    rec = store.register_use_if_valid(slug, now)
    if not rec:
        raise HTTPException(status_code=404, detail="Link expirado o inválido")

    passthrough_query = request.url.query
    internal_uri = _build_internal_redirect(rec.target_url, passthrough_query)
    response.headers["X-Accel-Redirect"] = internal_uri
    response.headers["Content-Disposition"] = _build_content_disposition(
        filename=rec.filename,
        target_url=rec.target_url,
        slug=rec.slug,
    )
    return Response(status_code=200, headers=response.headers)
