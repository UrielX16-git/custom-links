---
name: run-tests
description: >-
  Executes the custom-links automated test suite (pytest), syntax checks (compileall),
  Docker Compose configuration validation, and ALWAYS builds and tests the real Docker
  container end-to-end (file links via X-Accel-Redirect and dynamic mode="api" proxy links).
  Use this skill whenever the user asks to run tests, verify changes, check quality, or after
  modifying endpoints, the proxy, storage/migrations, models, Nginx config or the MCP bridge
  (e.g., "correr tests", "probar cambios", "verificar que todo funcione").
---

# custom-links Automated Testing & Docker E2E Verification Skill

This skill validates the custom-links service with `pytest` and **inside the real Docker image** (`python:3.12-slim`) to guarantee that FastAPI, SQLite migrations, the `httpx` streaming proxy, and the MCP bridge work in the production image that is published to Docker Hub (`urielx16/custom-links`).

**IMPORTANT RULE:** Never report that tests passed without building the Docker image and running the containerized verification steps below (Steps 2–4).

> [!NOTE]
> The host Python may be a very new version (e.g. 3.14) for which pinned dependencies (`pydantic-core`) have no wheels. The Docker image is the source of truth; the local venv step is optional.

---

## 1. Syntax Check & Optional Local Pytest

```powershell
python -m compileall -q app mcp tests .agents/skills/run-tests/scripts
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

# Opcional: solo si existe un venv funcional con las dependencias instaladas
if (Test-Path .\.venv\Scripts\python.exe) {
    .\.venv\Scripts\python.exe -m pytest -v
    if ($LASTEXITCODE -ne 0) { throw "local pytest failed" }
}
```

---

## 2. Docker Compose Validation & Container Build (Mandatory)

`docker-compose.yml` uses the published image (`urielx16/custom-links:latest`) without `build:`. The git-ignored `docker-compose.override.yml` adds `build: .` so the local source is built and tagged with that same image name.

Requirements before starting:
- `.env` exists (copy from `.env.example`) with `API_TOKEN`, `BASE_URL` and `SHARED_PROXY_NETWORK`.
- The external Docker network named in `SHARED_PROXY_NETWORK` exists (`docker network ls`). If not, create it for local testing: `docker network create <name>`.
- Port `8000` is free (other local stacks may also use it).

```powershell
# Ensure docker-compose.override.yml exists so local source is built
if (!(Test-Path docker-compose.override.yml)) {
    Set-Content -Path docker-compose.override.yml -Value "services:`n  custom-links:`n    build: .`n`n  custom-links-mcp:`n    build: .`n"
}

docker compose config -q
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed" }

docker compose up -d --build custom-links
if ($LASTEXITCODE -ne 0) { throw "docker compose build/up failed" }

# Opcional: también construir y levantar el puente MCP
docker compose --profile mcp up -d --build custom-links-mcp
```

---

## 3. Run Pytest Inside the Built Linux Image (Mandatory)

Tests are excluded from the image by `.dockerignore`, so mount them read-only:

```powershell
docker run --rm -v "${PWD}/tests:/app/tests:ro" -w /app urielx16/custom-links:latest sh -c "pip install -q pytest && python -m pytest -v -p no:cacheprovider tests"
if ($LASTEXITCODE -ne 0) { throw "container pytest failed" }
```

Also verify that the MCP server module loads inside the image (the local `mcp/` folder must not shadow the `mcp` pip package used by `fastmcp`):

```powershell
docker run --rm urielx16/custom-links:latest python -c "import runpy; m = runpy.run_path('/app/mcp/server.py', run_name='check'); print('MCP OK:', m['mcp'].name)"
if ($LASTEXITCODE -ne 0) { throw "MCP server import failed" }
```

---

## 4. Live Container End-to-End Test (Mandatory)

Runs against `http://localhost:8000` using only the Python standard library. It reads `API_TOKEN` from `.env` and verifies:
1. `/health` is up and the management API rejects invalid tokens.
2. A `mode="file"` link returns the expected `X-Accel-Redirect` + `Content-Disposition`, rejects `POST`, and returns `404` after revocation.
3. If `DYNAMIC_LINKS_ENABLED=true` (and `DYNAMIC_ALLOWED_HOSTS` allows `127.0.0.1:8000`): a `mode="api"` link that proxies to the service itself forwards subpaths, injects `Authorization`, masks injected headers, forwards `POST` bodies, and supports unlimited polling.

```powershell
python .agents/skills/run-tests/scripts/e2e_live.py --base http://localhost:8000 --env .env
if ($LASTEXITCODE -ne 0) { throw "live Docker E2E test failed" }
```

When finished, stop the local stack if it was not running before: `docker compose --profile mcp down`.

---

## 5. Adding New Features (Checklist)

Whenever an endpoint, link option or proxy behavior is added:
1. Keep `mode="file"` behavior backward-compatible (X-Accel-Redirect flow and existing SQLite rows).
2. Add new SQLite columns via the automatic migration in `app/storage.py` (`PRAGMA table_info` + `ALTER TABLE`).
3. Expose new link options in `mcp/server.py` (`create_link`) and document them in `README.md` and `.env.example`.
4. Add test cases in `tests/test_dynamic_links.py` (use `httpx.MockTransport` for upstream APIs).
5. Run Steps 1 through 4 above.
