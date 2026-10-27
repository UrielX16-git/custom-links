import os
import warnings
from typing import Annotated, Any, Dict, Optional

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
        "download filenames, and usage limits."
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
        Field(description="The original protected or internal destination URL to redirect to (must include http:// or https://)"),
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
        Field(description="Optional suggested filename to trigger browser download via Content-Disposition header (e.g. 'Video.mp4'). If omitted, it is inferred from custom_slug or target_url."),
    ] = None,
    max_uses: Annotated[
        Optional[int],
        Field(description="Optional maximum number of times the link can be opened or downloaded before expiring. If omitted, uses are unlimited until time expiration."),
    ] = None,
) -> Dict[str, Any]:
    """Create a new temporary short link pointing to a target URL with optional limits, custom slug (with optional file extension like 'Video.mp4'), or download filename."""
    payload = {
        "target_url": target_url,
        "expires_in": expires_in,
        "custom_slug": custom_slug,
        "filename": filename,
        "max_uses": max_uses,
    }
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
