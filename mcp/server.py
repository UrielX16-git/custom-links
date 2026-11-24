import os
import warnings
from typing import Annotated, Any, Dict, List, Literal, Optional

warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    from authlib.deprecate import AuthlibDeprecationWarning
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except ImportError:
    pass

from pydantic import Field
import httpx
from fastmcp import FastMCP


mcp = FastMCP(
    "custom-links-mcp",
    instructions=(
        "Use this server to create, inspect, and revoke temporary access links with "
        "customizable expiration times, custom slugs (supports file extensions like 'Video.mp4'), "
        "download filenames, usage limits, and optional dynamic API/MCP proxy mode (when enabled on the server)."
    ),
)


def _api_base() -> str:
    return os.getenv("MCP_API_BASE", "http://custom-links:8000").rstrip("/")


def _headers() -> Dict[str, str]:
    token = os.getenv("MCP_API_TOKEN", "")
    if not token:
        raise RuntimeError("MCP_API_TOKEN no configurado")
    return {"Authorization": f"Bearer {token}"}


@mcp.tool()
def create_link(
    target_url: Annotated[
        str,
        Field(description="The original protected or internal destination URL to redirect/proxy to (must include http:// or https://)"),
    ],
    expires_in: Annotated[
        Optional[int],
        Field(description="Expiration time in seconds. Set to 0 for permanent links (only use this value if the user explicitly asks for a permanent link). If omitted, uses server default. Maximum allowed when > 0 is 2592000 (30 days)."),
    ] = None,
    custom_slug: Annotated[
        Optional[str],
        Field(description="Optional custom slug in the public URL path (e.g. 'mi-video' or 'Video.mp4'). Supports alphanumeric characters, hyphens, underscores, and dots (e.g. for file extensions). If omitted, a secure random slug is generated."),
    ] = None,
    filename: Annotated[
        Optional[str],
        Field(description="Optional suggested filename to trigger browser download via Content-Disposition header (only applies to mode='file'). If omitted, it is inferred from custom_slug or target_url."),
    ] = None,
    max_uses: Annotated[
        Optional[int],
        Field(description="Optional maximum number of times the link can be opened or downloaded before expiring. If omitted, uses are unlimited until time expiration (recommended for mode='api')."),
    ] = None,
    mode: Annotated[
        Literal["file", "api"],
        Field(description="Link mode: 'file' (default, static files/streaming via Nginx X-Accel-Redirect) or 'api' (dynamic HTTP reverse proxy supporting all HTTP methods, subpaths, streaming/SSE, and MCP endpoints; requires DYNAMIC_LINKS_ENABLED=true on the server)."),
    ] = "file",
    allowed_methods: Annotated[
        Optional[List[str]],
        Field(description="Only for mode='api'. Allowed HTTP methods (e.g. ['GET', 'POST', 'DELETE']). If omitted, uses the server default."),
    ] = None,
    forward_subpaths: Annotated[
        Optional[bool],
        Field(description="Only for mode='api'. Whether subpaths like /s/{slug}/users/42 are forwarded to {target_url}/users/42. Defaults to true."),
    ] = None,
    inject_headers: Annotated[
        Optional[Dict[str, str]],
        Field(description="Only for mode='api'. Extra headers added/overridden on requests sent to target_url (e.g. internal Authorization tokens). Values are masked when inspected."),
    ] = None,
    cors_origins: Annotated[
        Optional[List[str]],
        Field(description="Only for mode='api'. Allowed browser CORS origins for this link (e.g. ['http://localhost:5173'] or ['*']). If omitted, CORS is delegated to the target API."),
    ] = None,
    count_preflight: Annotated[
        Optional[bool],
        Field(description="Only for mode='api'. Whether CORS preflight OPTIONS requests increment use_count. Defaults to false."),
    ] = None,
) -> Dict[str, Any]:
    """Create a new temporary short link (static file or dynamic API/MCP proxy) pointing to a target URL."""
    payload: Dict[str, Any] = {
        "target_url": target_url,
        "expires_in": expires_in,
        "custom_slug": custom_slug,
        "filename": filename,
        "max_uses": max_uses,
        "mode": mode,
    }
    if mode == "api":
        if allowed_methods is not None:
            payload["allowed_methods"] = allowed_methods
        if forward_subpaths is not None:
            payload["forward_subpaths"] = forward_subpaths
        if inject_headers is not None:
            payload["inject_headers"] = inject_headers
        if cors_origins is not None:
            payload["cors_origins"] = cors_origins
        if count_preflight is not None:
            payload["count_preflight"] = count_preflight

    with httpx.Client(timeout=10.0) as client:
        r = client.post(f"{_api_base()}/api/links", json=payload, headers=_headers())
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_link(
    slug: Annotated[
        str,
        Field(description="The unique identifier or slug of the link to inspect (e.g. 'my-video' or 'Ca2s-4d9P10')"),
    ],
) -> Dict[str, Any]:
    """Retrieve metadata, usage statistics, and expiration status of an existing link by its slug."""
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{_api_base()}/api/links/{slug}", headers=_headers())
    r.raise_for_status()
    return r.json()


@mcp.tool()
def revoke_link(
    slug: Annotated[
        str,
        Field(description="The unique identifier or slug of the link to revoke immediately"),
    ],
) -> Dict[str, Any]:
    """Immediately revoke an active link so that any further access to it returns 404 Not Found."""
    with httpx.Client(timeout=10.0) as client:
        r = client.delete(f"{_api_base()}/api/links/{slug}", headers=_headers())
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport in {"http", "streamable-http", "sse"}:
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8001"))
        path = os.getenv("MCP_PATH", "/mcp" if transport in {"http", "streamable-http"} else "/sse")
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            path=path,
            uvicorn_config={"ws": "none"},
        )
    else:
        mcp.run(transport="stdio", show_banner=False)
