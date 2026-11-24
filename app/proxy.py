from collections.abc import AsyncIterator
from typing import Optional
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
    }
)

CORS_RESPONSE_HEADERS = frozenset(
    {
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-allow-methods",
        "access-control-allow-headers",
        "access-control-expose-headers",
        "access-control-max-age",
    }
)

CORS_SAFELISTED_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-language",
        "content-length",
        "content-type",
        "expires",
        "last-modified",
        "pragma",
    }
)

# Umbral por debajo del cual leemos el cuerpo para enviar Content-Length exacto en lugar de chunked
_BUFFERED_BODY_THRESHOLD = 1024 * 1024  # 1 MB


class _BodyTooLargeError(Exception):
    pass


def create_proxy_client(timeout_seconds: float, read_timeout_seconds: float) -> httpx.AsyncClient:
    read_timeout = None if read_timeout_seconds <= 0 else read_timeout_seconds
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
            read=read_timeout,
        ),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )


def extract_raw_subpath(request: Request, slug: str) -> str:
    prefix = f"/internal-resolve/{slug}"
    raw_path_bytes = request.scope.get("raw_path")
    if raw_path_bytes:
        raw_path = raw_path_bytes.decode("latin-1")
        if raw_path.startswith(prefix):
            return raw_path[len(prefix) :]

    decoded_path = request.url.path
    if decoded_path.startswith(prefix):
        return decoded_path[len(prefix) :]
    return ""


def validate_subpath(raw_subpath: str) -> None:
    if not raw_subpath:
        return
    decoded = unquote(raw_subpath)
    if "\x00" in decoded or "\\" in decoded:
        raise HTTPException(status_code=400, detail="Subruta inválida")
    for candidate in (raw_subpath, decoded):
        if any(seg == ".." for seg in candidate.split("/")):
            raise HTTPException(status_code=400, detail="Subruta inválida")


def build_upstream_url(target_url: str, raw_subpath: str, passthrough_query: str) -> str:
    parsed = urlsplit(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="target_url inválido")

    base_path = parsed.path or ""
    if not raw_subpath:
        final_path = base_path or "/"
    elif raw_subpath == "/":
        final_path = base_path if base_path.endswith("/") else f"{base_path}/"
    else:
        suffix = raw_subpath if raw_subpath.startswith("/") else f"/{raw_subpath}"
        final_path = f"{base_path.rstrip('/')}{suffix}"

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

    base = f"{parsed.scheme}://{parsed.netloc}{final_path}"
    return f"{base}?{merged_query}" if merged_query else base


def is_cors_preflight(request: Request) -> bool:
    return (
        request.method.upper() == "OPTIONS"
        and bool(request.headers.get("origin"))
        and bool(request.headers.get("access-control-request-method"))
    )


def match_cors_origin(origin: str, cors_origins: list[str]) -> Optional[tuple[str, bool]]:
    normalized = origin.strip().rstrip("/")
    if not normalized:
        return None
    if normalized in cors_origins:
        return normalized, True
    if "*" in cors_origins:
        return "*", False
    return None


def build_cors_preflight_response(
    request: Request,
    cors_origins: list[str],
    allowed_methods: list[str],
) -> Response:
    origin = request.headers.get("origin", "")
    matched = match_cors_origin(origin, cors_origins)
    if not matched:
        return Response(status_code=403)

    req_method = (request.headers.get("access-control-request-method") or "").strip().upper()
    if req_method and req_method not in allowed_methods:
        return Response(status_code=405, headers={"Allow": ", ".join(allowed_methods)})

    allow_origin, allow_credentials = matched
    req_headers = request.headers.get("access-control-request-headers")
    headers = {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": ", ".join(allowed_methods),
        "Access-Control-Allow-Headers": req_headers if req_headers else "*",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
    }
    if allow_credentials:
        headers["Access-Control-Allow-Credentials"] = "true"
    return Response(status_code=204, headers=headers)


def _connection_header_tokens(headers: httpx.Headers | object) -> set[str]:
    tokens: set[str] = set()
    if hasattr(headers, "getlist"):
        values = headers.getlist("connection")
    else:
        val = headers.get("connection")  # type: ignore[union-attr]
        values = [val] if val else []
    for value in values:
        for part in value.split(","):
            token = part.strip().lower()
            if token:
                tokens.add(token)
    return tokens


def _build_upstream_headers(
    request: Request,
    inject_headers: Optional[dict[str, str]],
) -> list[tuple[str, str]]:
    extra_hop = _connection_header_tokens(request.headers)
    skip = HOP_BY_HOP_HEADERS | extra_hop | {"host", "content-length"}
    injected_lower = {k.lower() for k in inject_headers} if inject_headers else set()

    out: list[tuple[str, str]] = []
    has_accept_encoding = False
    has_xff = False
    has_xfp = False
    has_xfh = False

    for raw_k, raw_v in request.headers.raw:
        k = raw_k.decode("latin-1")
        v = raw_v.decode("latin-1")
        kl = k.lower()
        if kl in skip or kl in injected_lower:
            continue
        if kl == "accept-encoding":
            has_accept_encoding = True
        elif kl == "x-forwarded-for":
            has_xff = True
            if request.client and request.client.host:
                v = f"{v}, {request.client.host}"
        elif kl == "x-forwarded-proto":
            has_xfp = True
        elif kl == "x-forwarded-host":
            has_xfh = True
        out.append((k, v))

    # Evitar que httpx agregue Accept-Encoding: gzip implícito cuando el cliente no lo pidió
    if not has_accept_encoding and "accept-encoding" not in injected_lower:
        out.append(("Accept-Encoding", "identity"))

    if not has_xff and "x-forwarded-for" not in injected_lower and request.client and request.client.host:
        out.append(("X-Forwarded-For", request.client.host))
    if not has_xfp and "x-forwarded-proto" not in injected_lower:
        out.append(("X-Forwarded-Proto", request.url.scheme))
    if not has_xfh and "x-forwarded-host" not in injected_lower:
        host_header = request.headers.get("host")
        if host_header:
            out.append(("X-Forwarded-Host", host_header))

    if inject_headers:
        for k, v in inject_headers.items():
            out.append((k, v))

    return out


async def _prepare_request_body(
    request: Request,
    max_body_bytes: int,
) -> tuple[Optional[bytes], Optional[AsyncIterator[bytes]]]:
    method = request.method.upper()
    has_transfer_encoding = "transfer-encoding" in request.headers
    content_length_raw = request.headers.get("content-length")

    content_length: Optional[int] = None
    if content_length_raw is not None:
        try:
            content_length = int(content_length_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length inválido") from exc
        if content_length < 0:
            raise HTTPException(status_code=400, detail="Content-Length inválido")
        if max_body_bytes > 0 and content_length > max_body_bytes:
            raise HTTPException(status_code=413, detail="Cuerpo de petición demasiado grande")

    if method in {"GET", "HEAD", "OPTIONS"} and not has_transfer_encoding and not content_length:
        return None, None

    if not has_transfer_encoding and content_length == 0:
        return None, None

    if (
        not has_transfer_encoding
        and content_length is not None
        and content_length <= _BUFFERED_BODY_THRESHOLD
    ):
        body = await request.body()
        if max_body_bytes > 0 and len(body) > max_body_bytes:
            raise HTTPException(status_code=413, detail="Cuerpo de petición demasiado grande")
        return body, None

    async def _body_stream() -> AsyncIterator[bytes]:
        total = 0
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if max_body_bytes > 0 and total > max_body_bytes:
                raise _BodyTooLargeError()
            yield chunk

    return None, _body_stream()


def _apply_cors_to_response(
    response: Response,
    request: Request,
    cors_origins: list[str],
    exposed_header_names: list[str],
) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    matched = match_cors_origin(origin, cors_origins)
    if not matched:
        return

    allow_origin, allow_credentials = matched
    response.headers["Access-Control-Allow-Origin"] = allow_origin
    if allow_credentials:
        response.headers["Access-Control-Allow-Credentials"] = "true"
        if exposed_header_names:
            response.headers["Access-Control-Expose-Headers"] = ", ".join(exposed_header_names)
    else:
        response.headers["Access-Control-Expose-Headers"] = "*"

    existing_vary = response.headers.get("vary")
    if existing_vary:
        parts = [p.strip().lower() for p in existing_vary.split(",")]
        if "origin" not in parts:
            response.headers["Vary"] = f"{existing_vary}, Origin"
    else:
        response.headers["Vary"] = "Origin"


async def proxy_request(
    client: httpx.AsyncClient,
    request: Request,
    target_url: str,
    raw_subpath: str,
    inject_headers: Optional[dict[str, str]] = None,
    cors_origins: Optional[list[str]] = None,
    max_body_bytes: int = 0,
) -> Response:
    upstream_url = build_upstream_url(target_url, raw_subpath, request.url.query)
    upstream_headers = _build_upstream_headers(request, inject_headers)
    body_bytes, body_stream = await _prepare_request_body(request, max_body_bytes)

    content = body_bytes if body_bytes is not None else body_stream
    upstream_req = client.build_request(
        method=request.method,
        url=upstream_url,
        headers=upstream_headers,
        content=content,
    )

    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except _BodyTooLargeError as exc:
        raise HTTPException(status_code=413, detail="Cuerpo de petición demasiado grande") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Tiempo de espera agotado al contactar el destino") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Error de conexión con el destino") from exc

    async def _stream_response() -> AsyncIterator[bytes]:
        try:
            if upstream_resp.is_stream_consumed:
                if upstream_resp.content:
                    yield upstream_resp.content
            else:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
        finally:
            await upstream_resp.aclose()

    try:
        resp_skip = HOP_BY_HOP_HEADERS | _connection_header_tokens(upstream_resp.headers)
        if cors_origins:
            resp_skip = resp_skip | CORS_RESPONSE_HEADERS

        response = StreamingResponse(
            content=_stream_response(),
            status_code=upstream_resp.status_code,
        )

        exposed_names: list[str] = []
        exposed_lower: set[str] = set()
        for k, v in upstream_resp.headers.multi_items():
            kl = k.lower()
            if kl in resp_skip:
                continue
            response.headers.append(k, v)
            if kl not in CORS_SAFELISTED_RESPONSE_HEADERS and kl not in exposed_lower:
                exposed_lower.add(kl)
                exposed_names.append(k)

        # Desactivar buffering en Nginx para soportar SSE / MCP Streamable HTTP / streaming
        response.headers["X-Accel-Buffering"] = "no"

        if cors_origins:
            _apply_cors_to_response(response, request, cors_origins, exposed_names)

        return response
    except Exception:
        await upstream_resp.aclose()
        raise
