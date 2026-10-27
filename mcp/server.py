import os
from typing import Any, Dict, Optional

import httpx
from fastmcp import FastMCP


mcp = FastMCP("custom-links-mcp")


def _api_base() -> str:
    return os.getenv("MCP_API_BASE", "http://custom-links:8000").rstrip("/")


def _headers() -> Dict[str, str]:
    token = os.getenv("MCP_API_TOKEN", "")
    if not token:
        raise RuntimeError("MCP_API_TOKEN no configurado")
    return {"Authorization": f"Bearer {token}"}


@mcp.tool()
def create_link(
    target_url: str,
    expires_in: int = 3600,
    custom_slug: Optional[str] = None,
    filename: Optional[str] = None,
    max_uses: Optional[int] = None,
) -> Dict[str, Any]:
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
def get_link(slug: str) -> Dict[str, Any]:
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{_api_base()}/api/links/{slug}", headers=_headers())
    r.raise_for_status()
    return r.json()


@mcp.tool()
def revoke_link(slug: str) -> Dict[str, Any]:
    with httpx.Client(timeout=10.0) as client:
        r = client.delete(f"{_api_base()}/api/links/{slug}", headers=_headers())
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport in {"sse", "http", "streamable-http"}:
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8001"))
        mcp.run(transport=transport, host=host, port=port)
    else:
        mcp.run(transport="stdio", show_banner=False)
