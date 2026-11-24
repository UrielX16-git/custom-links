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

from . import __version__
from .config import settings
from .models import LinkCreateRequest, LinkCreateResponse, LinkInfoResponse
from .proxy import (
    build_cors_preflight_response,
    create_proxy_client,
    extract_raw_subpath,
    is_cors_preflight,
    proxy_request,
    validate_subpath,
)
from .storage import LinkRecord, LinkStore

logger = logging.getLogger("uvicorn.error")

store = LinkStore(settings.db_path)
security = HTTPBearer(auto_error=False)

_ALL_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class _ScopedCORSMiddleware:
    """Aplica el CORS global a la API de gestión (/api/*, /health), sin interferir con /internal-resolve/*."""

    def __init__(self, app, **cors_kwargs):
        self.app = app
        self.cors_app = CORSMiddleware(app, **cors_kwargs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/internal-resolve/"):
            await self.app(scope, receive, send)
        else:
            await self.cors_app(scope, receive, send)


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
    app.state.proxy_client = create_proxy_client(
        timeout_seconds=settings.dynamic_timeout_seconds,
        read_timeout_seconds=settings.dynamic_read_timeout_seconds,
    )

    cleanup_task = None
    if settings.auto_cleanup_enabled:
        cleanup_task = asyncio.create_task(_periodic_cleanup_loop(settings.cleanup_interval_seconds))
        logger.info(
            f"[AutoCleanup] Tarea de limpieza automática iniciada (intervalo: {settings.cleanup_interval_seconds}s)"
        )

    if settings.dynamic_links_enabled:
        hosts_desc = ", ".join(settings.dynamic_allowed_hosts_list) or "cualquiera"
        logger.info(f"[DynamicLinks] Modo API habilitado (hosts permitidos: {hosts_desc})")

    try:
        yield
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        await app.state.proxy_client.aclose()


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)

# Configuración de CORS (acotado a la API de gestión; los links dinámicos gestionan su propio CORS)
origins = settings.cors_origins_list
allow_all = "*" in origins
app.add_middleware(
    _ScopedCORSMiddleware,
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


def _record_to_info(rec: LinkRecord) -> LinkInfoResponse:
    opts = rec.options or {}
    raw_headers = opts.get("inject_headers")
    masked_headers = {k: "***" for k in raw_headers} if isinstance(raw_headers, dict) and raw_headers else None

    return LinkInfoResponse(
        slug=rec.slug,
        target_url=rec.target_url,
        expires_at=rec.expires_at,
        revoked=bool(rec.revoked),
        filename=rec.filename,
        max_uses=rec.max_uses,
        use_count=rec.use_count,
        created_at=rec.created_at,
        last_access_at=rec.last_access_at,
        mode=rec.mode,
        allowed_methods=opts.get("allowed_methods") if rec.mode == "api" else None,
        forward_subpaths=opts.get("forward_subpaths") if rec.mode == "api" else None,
        inject_headers=masked_headers if rec.mode == "api" else None,
        cors_origins=opts.get("cors_origins") if rec.mode == "api" else None,
        count_preflight=opts.get("count_preflight") if rec.mode == "api" else None,
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "dynamic_links_enabled": settings.dynamic_links_enabled}


@app.post("/api/links", response_model=LinkCreateResponse, dependencies=[Depends(_require_api_token)])
def create_link(req: LinkCreateRequest) -> LinkCreateResponse:
    target_url = str(req.target_url)

    if req.mode == "api":
        if not settings.dynamic_links_enabled:
            raise HTTPException(
                status_code=403,
                detail="Los enlaces dinámicos (mode='api') están deshabilitados en el servidor",
            )
        if not settings.is_dynamic_host_allowed(target_url):
            raise HTTPException(
                status_code=403,
                detail="Host de destino no permitido por DYNAMIC_ALLOWED_HOSTS",
            )

    if req.expires_in == 0:
        expires_at = 0
    else:
        raw_expires = req.expires_in if req.expires_in is not None else settings.default_expires_in
        expires_in = min(raw_expires, settings.max_expires_in)
        expires_at = int(time.time()) + expires_in

    slug = req.custom_slug or secrets.token_urlsafe(8)
    if len(slug) > settings.max_slug_len:
        raise HTTPException(status_code=400, detail="Slug demasiado largo")

    filename: Optional[str] = None
    options: Optional[dict] = None

    if req.mode == "file":
        filename = req.filename
        # Si no se pasó filename explícito pero el custom_slug tiene extensión (ej. Video.mp4),
        # usarlo como nombre de descarga
        if not filename and req.custom_slug and "." in req.custom_slug:
            filename = req.custom_slug
    else:
        options = {
            "allowed_methods": req.allowed_methods or settings.dynamic_default_methods_list,
            "forward_subpaths": True if req.forward_subpaths is None else req.forward_subpaths,
            "inject_headers": req.inject_headers,
            "cors_origins": req.cors_origins,
            "count_preflight": False if req.count_preflight is None else req.count_preflight,
        }

    try:
        store.create_link(
            slug=slug,
            target_url=target_url,
            expires_at=expires_at,
            filename=filename,
            max_uses=req.max_uses,
            mode=req.mode,
            options=options,
        )
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="Slug en uso") from exc
        raise

    return LinkCreateResponse(
        slug=slug,
        url=_build_public_url(slug),
        expires_at=expires_at,
        mode=req.mode,
    )


@app.get("/api/links/{slug}", response_model=LinkInfoResponse, dependencies=[Depends(_require_api_token)])
def get_link(slug: str) -> LinkInfoResponse:
    rec = store.get_link(slug)
    if not rec:
        raise HTTPException(status_code=404, detail="No existe")
    return _record_to_info(rec)


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


async def _handle_resolve(slug: str, request: Request) -> Response:
    now = int(time.time())
    rec = await asyncio.to_thread(store.get_valid_link, slug, now)
    if not rec:
        raise HTTPException(status_code=404, detail="Link expirado o inválido")

    raw_subpath = extract_raw_subpath(request, slug)

    # Flujo clásico de archivos estáticos vía X-Accel-Redirect
    if rec.mode != "api":
        if raw_subpath:
            raise HTTPException(status_code=404, detail="Link expirado o inválido")
        if request.method.upper() not in {"GET", "HEAD"}:
            return Response(status_code=405, headers={"Allow": "GET, HEAD"})

        rec = await asyncio.to_thread(store.register_use_if_valid, slug, now)
        if not rec:
            raise HTTPException(status_code=404, detail="Link expirado o inválido")

        internal_uri = _build_internal_redirect(rec.target_url, request.url.query)
        headers = {
            "X-Accel-Redirect": internal_uri,
            "Content-Disposition": _build_content_disposition(
                filename=rec.filename,
                target_url=rec.target_url,
                slug=rec.slug,
            ),
        }
        return Response(status_code=200, headers=headers)

    # Flujo dinámico (API / MCP / polling)
    if not settings.dynamic_links_enabled:
        raise HTTPException(status_code=404, detail="Link expirado o inválido")
    if not settings.is_dynamic_host_allowed(rec.target_url):
        raise HTTPException(status_code=403, detail="Host de destino no permitido")

    opts = rec.options or {}
    allowed_methods = opts.get("allowed_methods") or settings.dynamic_default_methods_list
    forward_subpaths = opts.get("forward_subpaths", True)
    inject_headers = opts.get("inject_headers")
    cors_origins = opts.get("cors_origins")
    count_preflight = opts.get("count_preflight", False)

    if raw_subpath and not forward_subpaths:
        raise HTTPException(status_code=404, detail="Subrutas no habilitadas para este enlace")

    validate_subpath(raw_subpath)

    preflight = is_cors_preflight(request)
    if preflight and cors_origins:
        if count_preflight:
            rec = await asyncio.to_thread(store.register_use_if_valid, slug, now)
            if not rec:
                raise HTTPException(status_code=404, detail="Link expirado o inválido")
        return build_cors_preflight_response(request, cors_origins, allowed_methods)

    if request.method.upper() not in allowed_methods:
        return Response(status_code=405, headers={"Allow": ", ".join(allowed_methods)})

    if not (preflight and not count_preflight):
        rec = await asyncio.to_thread(store.register_use_if_valid, slug, now)
        if not rec:
            raise HTTPException(status_code=404, detail="Link expirado o inválido")

    client = getattr(request.app.state, "proxy_client", None)
    if client is None:
        client = create_proxy_client(
            timeout_seconds=settings.dynamic_timeout_seconds,
            read_timeout_seconds=settings.dynamic_read_timeout_seconds,
        )
        request.app.state.proxy_client = client

    return await proxy_request(
        client=client,
        request=request,
        target_url=rec.target_url,
        raw_subpath=raw_subpath,
        inject_headers=inject_headers,
        cors_origins=cors_origins,
        max_body_bytes=settings.dynamic_max_body_bytes,
    )


@app.api_route("/internal-resolve/{slug}", methods=_ALL_HTTP_METHODS)
async def resolve_link(slug: str, request: Request):
    return await _handle_resolve(slug, request)


@app.api_route("/internal-resolve/{slug}/{subpath:path}", methods=_ALL_HTTP_METHODS)
async def resolve_link_with_subpath(slug: str, subpath: str, request: Request):
    return await _handle_resolve(slug, request)
