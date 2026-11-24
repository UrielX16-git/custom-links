import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient


def _load_app(tmp_path: Path, **env_overrides: str):
    db_path = tmp_path / "test_links.db"
    env = {
        "API_TOKEN": "test-secret-token",
        "BASE_URL": "https://links.example.com",
        "DB_PATH": str(db_path),
        "AUTO_CLEANUP_ENABLED": "false",
        "DYNAMIC_LINKS_ENABLED": "false",
        "DYNAMIC_ALLOWED_HOSTS": "",
        "DYNAMIC_DEFAULT_METHODS": "GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS",
        "DYNAMIC_TIMEOUT_SECONDS": "10",
        "DYNAMIC_READ_TIMEOUT_SECONDS": "0",
        "DYNAMIC_MAX_BODY_BYTES": "1048576",
    }
    env.update(env_overrides)
    for key, val in env.items():
        os.environ[key] = val

    for mod_name in ("app.main", "app.proxy", "app.storage", "app.models", "app.config"):
        sys.modules.pop(mod_name, None)

    config_mod = importlib.import_module("app.config")
    main_mod = importlib.import_module("app.main")
    return config_mod, main_mod


AUTH_HEADERS = {"Authorization": "Bearer test-secret-token"}


def _mock_upstream_client():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body_bytes = request.read()
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body_bytes.decode("utf-8", errors="replace"),
            }
        )

        if request.url.path.endswith("/sse"):
            class _SSEStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"event: message\n"
                    yield b'data: {"ok":true}\n\n'

                async def aclose(self):
                    pass

            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Mcp-Session-Id": "sess-12345",
                    "Access-Control-Allow-Origin": "https://upstream.should.be.replaced",
                },
                stream=_SSEStream(),
            )

        return httpx.Response(
            status_code=201 if request.method == "POST" else 200,
            headers={
                "Content-Type": "application/json",
                "Mcp-Session-Id": "sess-999",
                "X-Upstream": "yes",
            },
            content=json.dumps(
                {
                    "echo_method": request.method,
                    "echo_path": request.url.path,
                    "echo_query": request.url.query.decode("utf-8"),
                    "echo_auth": request.headers.get("authorization"),
                    "echo_custom": request.headers.get("x-internal"),
                    "echo_body": body_bytes.decode("utf-8", errors="replace"),
                }
            ).encode("utf-8"),
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    return client, captured


def test_dynamic_disabled_by_default(tmp_path: Path):
    _, main_mod = _load_app(tmp_path, DYNAMIC_LINKS_ENABLED="false")
    with TestClient(main_mod.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["dynamic_links_enabled"] is False
        assert body["version"]

        r = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={"target_url": "http://10.0.0.5:8001/mcp", "mode": "api"},
        )
        assert r.status_code == 403


def test_file_mode_preserves_behavior_and_blocks_subpath_or_post(tmp_path: Path):
    _, main_mod = _load_app(tmp_path, DYNAMIC_LINKS_ENABLED="true")
    with TestClient(main_mod.app) as client:
        # API-only options rejected on file mode
        bad = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={
                "target_url": "http://10.0.0.5:5000/video.mp4",
                "mode": "file",
                "inject_headers": {"X-Test": "1"},
            },
        )
        assert bad.status_code == 422

        r = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={
                "target_url": "http://10.0.0.5:5000/output/video.mp4?sig=abc",
                "custom_slug": "clip.mp4",
                "max_uses": 2,
            },
        )
        assert r.status_code == 200
        assert r.json()["mode"] == "file"

        # Subpath on file link -> 404 and does not increment use_count
        r_sub = client.get("/internal-resolve/clip.mp4/extra")
        assert r_sub.status_code == 404

        # POST on file link -> 405 and does not increment use_count
        r_post = client.post("/internal-resolve/clip.mp4")
        assert r_post.status_code == 405
        assert "GET" in r_post.headers["allow"]

        # Valid GET -> X-Accel-Redirect + Content-Disposition
        r_ok = client.get("/internal-resolve/clip.mp4?dl=1")
        assert r_ok.status_code == 200
        assert r_ok.headers["x-accel-redirect"] == "/_internal_proxy/http/10.0.0.5:5000/output/video.mp4?sig=abc&dl=1"
        assert 'filename="clip.mp4"' in r_ok.headers["content-disposition"]

        info = client.get("/api/links/clip.mp4", headers=AUTH_HEADERS).json()
        assert info["use_count"] == 1
        assert info["mode"] == "file"


def test_sqlite_migration_from_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "test_links.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE links (
            slug TEXT PRIMARY KEY,
            target_url TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            filename TEXT,
            max_uses INTEGER,
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            last_access_at INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO links (slug, target_url, expires_at, created_at) VALUES (?, ?, ?, ?)",
        ("legacy-file", "http://10.0.0.5/doc.pdf", 0, 1700000000),
    )
    conn.commit()
    conn.close()

    _, main_mod = _load_app(tmp_path, DYNAMIC_LINKS_ENABLED="true")
    with TestClient(main_mod.app) as client:
        info = client.get("/api/links/legacy-file", headers=AUTH_HEADERS)
        assert info.status_code == 200
        data = info.json()
        assert data["mode"] == "file"
        assert data["allowed_methods"] is None

        res = client.get("/internal-resolve/legacy-file")
        assert res.status_code == 200
        assert res.headers["x-accel-redirect"] == "/_internal_proxy/http/10.0.0.5/doc.pdf"


def test_api_mode_proxy_methods_subpaths_headers_sse_and_cors(tmp_path: Path):
    _, main_mod = _load_app(tmp_path, DYNAMIC_LINKS_ENABLED="true", DYNAMIC_MAX_BODY_BYTES="256")
    mock_client, captured = _mock_upstream_client()

    with TestClient(main_mod.app) as client:
        main_mod.app.state.proxy_client = mock_client

        create = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={
                "target_url": "http://10.0.0.5:8001/mcp?env=dev",
                "mode": "api",
                "custom_slug": "mcp-dev",
                "allowed_methods": ["GET", "POST", "DELETE"],
                "forward_subpaths": True,
                "inject_headers": {
                    "Authorization": "Bearer super-secret-internal",
                    "X-Internal": "from-proxy",
                },
                "cors_origins": ["http://localhost:5173"],
                "count_preflight": False,
            },
        )
        assert create.status_code == 200
        assert create.json()["mode"] == "api"

        # Inspecting masks injected header values
        info = client.get("/api/links/mcp-dev", headers=AUTH_HEADERS).json()
        assert info["mode"] == "api"
        assert info["inject_headers"] == {"Authorization": "***", "X-Internal": "***"}
        assert info["filename"] is None

        # Preflight OPTIONS with allowed origin -> 204, use_count stays 0
        pre_ok = client.options(
            "/internal-resolve/mcp-dev/tools",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Mcp-Session-Id",
            },
        )
        assert pre_ok.status_code == 204
        assert pre_ok.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert pre_ok.headers["access-control-allow-credentials"] == "true"
        assert "Mcp-Session-Id" in pre_ok.headers["access-control-allow-headers"]
        assert client.get("/api/links/mcp-dev", headers=AUTH_HEADERS).json()["use_count"] == 0

        # Preflight OPTIONS with disallowed origin -> 403
        pre_bad = client.options(
            "/internal-resolve/mcp-dev",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert pre_bad.status_code == 403

        # POST JSON with subpath, query, and spoofed Authorization header (must be overridden)
        r_post = client.post(
            "/internal-resolve/mcp-dev/tools/call?debug=1",
            headers={
                "Origin": "http://localhost:5173",
                "Authorization": "Bearer spoofed-client-token",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        assert r_post.status_code == 201
        assert "content-disposition" not in r_post.headers
        assert r_post.headers["x-accel-buffering"] == "no"
        assert r_post.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "mcp-session-id" in r_post.headers["access-control-expose-headers"].lower()
        payload = r_post.json()
        assert payload["echo_method"] == "POST"
        assert payload["echo_path"] == "/mcp/tools/call"
        assert payload["echo_query"] == "env=dev&debug=1"
        assert payload["echo_auth"] == "Bearer super-secret-internal"
        assert payload["echo_custom"] == "from-proxy"
        assert '"tools/list"' in payload["echo_body"]

        # SSE response streaming + replacing upstream CORS header
        r_sse = client.get(
            "/internal-resolve/mcp-dev/sse",
            headers={"Origin": "http://localhost:5173", "Accept": "text/event-stream"},
        )
        assert r_sse.status_code == 200
        assert r_sse.headers["content-type"].startswith("text/event-stream")
        assert r_sse.headers["mcp-session-id"] == "sess-12345"
        assert r_sse.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "event: message" in r_sse.text

        # Disallowed method (PUT) -> 405, does not increment use_count
        r_put = client.put("/internal-resolve/mcp-dev", json={"x": 1})
        assert r_put.status_code == 405
        assert "POST" in r_put.headers["allow"]

        # Path traversal rejected -> 400
        r_trav = client.get("/internal-resolve/mcp-dev/%2e%2e/secret")
        assert r_trav.status_code == 400

        # Body larger than DYNAMIC_MAX_BODY_BYTES (256) -> 413
        r_big = client.post("/internal-resolve/mcp-dev", content=b"x" * 300)
        assert r_big.status_code == 413

        # Unlimited polling: 2 successful calls + 1 failed 413 (already registered before body check)
        assert len(captured) == 2

        # Revoking stops access immediately
        assert client.delete("/api/links/mcp-dev", headers=AUTH_HEADERS).status_code == 200
        assert client.get("/internal-resolve/mcp-dev").status_code == 404


def test_allowed_hosts_and_no_subpaths(tmp_path: Path):
    _, main_mod = _load_app(
        tmp_path,
        DYNAMIC_LINKS_ENABLED="true",
        DYNAMIC_ALLOWED_HOSTS="10.0.0.5:8001,*.internal",
    )
    mock_client, _ = _mock_upstream_client()

    with TestClient(main_mod.app) as client:
        main_mod.app.state.proxy_client = mock_client

        # Disallowed host
        r_bad_host = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={"target_url": "http://192.168.1.99:8001/api", "mode": "api"},
        )
        assert r_bad_host.status_code == 403

        # Wrong port on host:port rule
        r_bad_port = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={"target_url": "http://10.0.0.5:9999/api", "mode": "api"},
        )
        assert r_bad_port.status_code == 403

        # Exact host:port allowed
        r_ok1 = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={
                "target_url": "http://10.0.0.5:8001/api",
                "mode": "api",
                "custom_slug": "strict-api",
                "forward_subpaths": False,
            },
        )
        assert r_ok1.status_code == 200

        # Wildcard subdomain allowed
        r_ok2 = client.post(
            "/api/links",
            headers=AUTH_HEADERS,
            json={
                "target_url": "http://svc.internal:3000/v1",
                "mode": "api",
                "custom_slug": "wild-api",
            },
        )
        assert r_ok2.status_code == 200

        # forward_subpaths=False rejects subpaths
        assert client.get("/internal-resolve/strict-api").status_code == 200
        assert client.get("/internal-resolve/strict-api/extra").status_code == 404
