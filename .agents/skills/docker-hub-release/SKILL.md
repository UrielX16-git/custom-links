---
name: docker-hub-release
description: >-
  Audits the custom-links project for multi-architecture, Docker Compose, security, and dependency
  issues, bumps the version in app/__init__.py, creates the release commit and git tag, and provides
  the push command that triggers the automated Docker Hub GitHub Actions workflow
  (urielx16/custom-links). Use this skill whenever the user asks to publish, release, deploy, or push
  changes to Docker Hub (e.g., "publicar en Docker Hub", "subir nueva versión", "preparar release").
---

# Docker Hub Release & Pre-Publish Audit Workflow

This project automatically builds and publishes a multi-architecture Docker image (`urielx16/custom-links`) to Docker Hub via GitHub Actions (`.github/workflows/docker-publish.yml`) whenever commits are pushed to `main` or a semantic version tag (`v*.*.*`) is pushed. The workflow first runs `pytest` (job `test`) and only publishes if it passes.

The same image is used by both services in `docker-compose.yml`:
- `custom-links` → default `CMD` (uvicorn `app.main:app` on port 8000).
- `custom-links-mcp` → `command: python /app/mcp/server.py` (profile `mcp`, port 8001).

Follow these steps strictly in order whenever the user wants to prepare or publish a new version.

---

## 0. First-Time Setup (only if the image has never been published)

Verify with the user that these one-time prerequisites are done; the workflow fails at login otherwise:
1. **Docker Hub access token**: Docker Hub → Account settings → Personal access tokens → create one with **Read & Write** scope.
2. **GitHub repository secrets** (`UrielX16-git/custom-links` → Settings → Secrets and variables → Actions):
   - `DOCKER_USERNAME` = `urielx16`
   - `DOCKER_PASSWORD` = the access token (never the account password, never committed to any file).
   Secrets are per repository: secrets configured in other repos (e.g. `procesamiento-multimedia-api-docker`) are **not** shared.
3. **Docker Hub repository**: pushing with a Read & Write token auto-creates `urielx16/custom-links` as **public**. If it must be private, create it manually in Docker Hub before the first push.
4. After the first successful run, confirm tags at `https://hub.docker.com/r/urielx16/custom-links/tags`.

---

## 1. Pre-Release Technical Audit (Mandatory)

Before bumping the version or creating any commit/tag, inspect all uncommitted and unpushed changes (`git status`, `git diff`, `git log origin/main..HEAD`) and verify every item in this checklist. Fix any issues found before proceeding.

### A. Multi-Architecture Compatibility (`linux/amd64` & `linux/arm64`)
The workflow builds for `platforms: linux/amd64,linux/arm64` using QEMU and Docker Buildx.
1. **`Dockerfile`**: If any architecture-specific binary is ever downloaded, declare `ARG TARGETARCH` and interpolate `${TARGETARCH}`; never hardcode `amd64`/`x86_64`.
2. **`requirements.txt`**: New packages must ship wheels for `x86_64` and `aarch64` on Python 3.12 (`python:3.12-slim`), or the `Dockerfile` must install and clean up build tools (`gcc`, `python3-dev`).

### B. Docker Compose Production vs. Local Override
1. **`docker-compose.yml` (Production Ready)**:
   - Both `custom-links` and `custom-links-mcp` must use `image: urielx16/custom-links:latest`.
   - Must **NOT** contain `build: .`, so users with only `docker-compose.yml` + `.env` can run `docker compose up -d`.
   - New environment variables must be documented in `.env.example` (both services read `.env` via `env_file`).
2. **`docker-compose.override.yml` (Local Development)**:
   - Must contain `build: .` for `custom-links` and `custom-links-mcp`.
   - Must be listed in `.gitignore` and `.dockerignore`.

### C. Volume Mounts & Data Safety
1. SQLite lives in the `./data:/data` bind mount (`DB_PATH=/data/links.db`). Never bake `data/`, `*.db`, `*.db-wal` or `*.db-shm` into the image or the repo.
2. Schema changes must be additive and applied by the automatic migration in `app/storage.py`, so existing databases keep working after `docker compose pull`.
3. If `nginx/npm-advanced.conf` changed, the release notes/handoff must tell the user to paste it again into the NPM Proxy Host **Advanced** tab.

### D. Strict Secret, Token & Sensitive Data Audit (ZERO TOLERANCE — BLOCKING)
**CRITICAL RULE**: Under NO circumstances may any API key, personal access token (PAT), `API_TOKEN`, `MCP_API_TOKEN`, password, private key, or credential ever be staged, committed, or baked into a Docker image. If ANY secret is detected in a tracked or staged file, **ABORT the release immediately**, remove/redact the secret, and warn the user. This includes comments inside git-ignored files that could be copied elsewhere (e.g. a Docker PAT written in `docker-compose.override.yml`): warn the user to remove it and revoke the token.

1. **`.gitignore` and `.dockerignore` Verification**:
   - Sensitive files (`.env`, `.env.*`, `docker-compose.override.yml`, `*.pem`, `*.key`, `*credentials*.json`) and runtime data (`data/`, `*.db*`) must be excluded in **both** files (`.env.example` must stay tracked).
   - Confirm Git ignores them:
     ```powershell
     git check-ignore -v .env docker-compose.override.yml data/links.db
     ```
2. **Automated Secret Scan on Tracked, Staged & Untracked Project Files**:
   ```powershell
   $files = git ls-files --cached --others --exclude-standard
   if ($files) {
       Select-String -Path $files -Pattern '(dckr_pat_|ya29\.|GOCSPX-|AIza|ghp_|github_pat_|glpat-|sk-[a-zA-Z0-9]{20,}|1//[0-9a-zA-Z_-]{20,}|access_token|refresh_token|client_secret|api_key|apikey|password|secret|API_TOKEN\s*=\s*\S+|token\s*=)' -CaseSensitive:$false
   }
   ```
   Inspect every match. Expected harmless matches: placeholders in `.env.example` (`replace_with_random_secret`, `replace_with_same_api_token`), `${{ secrets.* }}` references in the workflow, `<API_TOKEN>` in `README.md`, and fake tokens in `tests/` (`test-secret-token`, `super-secret-internal`). Anything else that looks real is BLOCKING.
3. **Pre-Commit Staged Diff Inspection**: always run `git diff --cached` and review it line by line before `git commit`.

### E. Syntax, Config & Automated Docker Tests Validation
Execute the complete `run-tests` skill (`.agents/skills/run-tests/SKILL.md`): `compileall`, `docker compose config`, `docker compose up -d --build`, `pytest` inside the Linux image, MCP import check, and the live E2E script. **Do not proceed if any check fails.**

---

## 2. Version Bump

1. Check the current version in `app/__init__.py` (`__version__ = "X.Y.Z"`, also reported by `GET /health`) and existing tags (`git tag --sort=-v:refname`).
2. Choose the next semantic version if the user did not specify one:
   - **Patch (`X.Y.Z+1`)**: bug fixes, Nginx/Dockerfile tweaks, internal refactors.
   - **Minor (`X.Y+1.0`)**: new endpoints, new link options/modes, new env variables (backward-compatible).
   - **Major (`X+1.0.0`)**: breaking API changes or non-backward-compatible schema/config changes.
   - **First release**: if no `v*` tag exists yet, release the current `__version__` (default `1.0.0`) without bumping.
3. Update `__version__` in `app/__init__.py`.

---

## 3. Commit & Tag Creation

1. Stage files explicitly (`git add <files>` — **avoid `git add .` / `git add -A`**), then check `git status` and `git diff --cached` so no temporary files, databases or secrets are included.
2. Commit with the version as prefix:
   ```powershell
   git commit -m "vX.Y.Z: <concise description of changes>"
   ```
3. Create the matching tag (must start with `v` to match the `v*.*.*` trigger):
   ```powershell
   git tag vX.Y.Z
   ```
   *(If `vX.Y.Z` exists locally and hasn't been pushed, update it with `git tag -f vX.Y.Z`.)*

---

## 4. Handoff to User

Provide a brief summary including:
1. **Audit Results**: issues detected and fixed, or confirmation that multi-arch, compose, secrets and test checks passed.
2. **Version & Tag**: version in `app/__init__.py`, commit hash and tag `vX.Y.Z`.
3. **Push Command** (triggers GitHub Actions):
   ```bash
   git push origin main --tags
   ```
4. **Expected Docker Hub Output**:
   - `urielx16/custom-links:latest` (push to `main`)
   - `urielx16/custom-links:X.Y.Z` and `urielx16/custom-links:X.Y` (tag push)
5. **Server Update Commands**:
   ```bash
   docker compose pull && docker compose up -d
   docker compose --profile mcp pull && docker compose --profile mcp up -d   # if MCP is used
   ```
