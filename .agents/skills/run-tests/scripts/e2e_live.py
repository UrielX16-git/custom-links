"""
Prueba End-to-End contra el contenedor real de custom-links (http://localhost:8000).

Solo usa la librería estándar de Python para poder ejecutarse con cualquier intérprete
del host (sin instalar dependencias).

Uso:
    python .agents/skills/run-tests/scripts/e2e_live.py [--base http://localhost:8000] [--env .env]
"""
import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: Path) -> dict:
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def request(method: str, url: str, token: str = None, body: dict = None, timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def as_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def check(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[FAIL] {msg}")
        sys.exit(1)
    print(f"[OK]   {msg}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    env = load_env(Path(args.env))
    token = env.get("API_TOKEN")
    check(bool(token), f"API_TOKEN leído desde {args.env}")

    # 1. Esperar a que el contenedor esté listo
    health = None
    for _ in range(30):
        try:
            status, _, raw = request("GET", f"{base}/health", timeout=3)
            if status == 200:
                health = as_json(raw)
                break
        except Exception:
            pass
        time.sleep(1)
    check(health is not None and health.get("ok") is True, f"/health responde: {health}")

    created: list[str] = []
    suffix = secrets.token_hex(4)

    try:
        # 2. Auth requerida
        status, _, _ = request("POST", f"{base}/api/links", token="token-invalido", body={"target_url": "http://example.com"})
        check(status == 401, "API de gestión rechaza token inválido (401)")

        # 3. Enlace de archivo (mode=file) -> X-Accel-Redirect
        file_slug = f"e2e-file-{suffix}.txt"
        status, _, raw = request(
            "POST",
            f"{base}/api/links",
            token=token,
            body={"target_url": "http://10.255.255.1:5000/data/report.txt?sig=abc", "custom_slug": file_slug, "expires_in": 300},
        )
        check(status == 200 and as_json(raw).get("mode") == "file", f"Crear enlace file ({status})")
        created.append(file_slug)

        status, headers, _ = request("GET", f"{base}/internal-resolve/{file_slug}?dl=1")
        headers_l = {k.lower(): v for k, v in headers.items()}
        check(
            status == 200
            and headers_l.get("x-accel-redirect") == "/_internal_proxy/http/10.255.255.1:5000/data/report.txt?sig=abc&dl=1",
            f"Resolver enlace file devuelve X-Accel-Redirect ({headers_l.get('x-accel-redirect')})",
        )
        check('filename="' in headers_l.get("content-disposition", ""), "Content-Disposition presente en modo file")

        status, _, _ = request("POST", f"{base}/internal-resolve/{file_slug}")
        check(status == 405, "Modo file rechaza POST (405)")

        # 4. Enlace dinámico (mode=api) -> proxy hacia el propio servicio
        if not health.get("dynamic_links_enabled"):
            print("[SKIP] DYNAMIC_LINKS_ENABLED=false: se omiten pruebas de mode='api'")
        else:
            api_slug = f"e2e-api-{suffix}"
            status, _, raw = request(
                "POST",
                f"{base}/api/links",
                token=token,
                body={
                    "target_url": "http://127.0.0.1:8000",
                    "mode": "api",
                    "custom_slug": api_slug,
                    "expires_in": 300,
                    "inject_headers": {"Authorization": f"Bearer {token}"},
                },
            )
            if status == 403:
                print(f"[SKIP] DYNAMIC_ALLOWED_HOSTS no permite 127.0.0.1:8000: {as_json(raw)}")
            else:
                check(status == 200 and as_json(raw).get("mode") == "api", f"Crear enlace api ({status})")
                created.append(api_slug)

                # GET con subruta proxied hacia /health
                status, _, raw = request("GET", f"{base}/internal-resolve/{api_slug}/health")
                check(status == 200 and as_json(raw).get("ok") is True, "Proxy GET con subruta (/health)")

                # GET protegido usando el header inyectado (sin enviar token desde el cliente)
                status, _, raw = request("GET", f"{base}/internal-resolve/{api_slug}/api/links/{api_slug}")
                info = as_json(raw) or {}
                check(
                    status == 200 and info.get("mode") == "api" and info.get("inject_headers") == {"Authorization": "***"},
                    "Proxy inyecta Authorization y la consulta enmascara headers",
                )

                # POST con body JSON a través del proxy
                nested_slug = f"e2e-nested-{suffix}"
                status, _, raw = request(
                    "POST",
                    f"{base}/internal-resolve/{api_slug}/api/links",
                    body={"target_url": "http://10.255.255.1/x.bin", "custom_slug": nested_slug, "expires_in": 60},
                )
                check(status == 200 and as_json(raw).get("slug") == nested_slug, "Proxy POST reenvía método y body")
                created.append(nested_slug)

                # Uso ilimitado (polling)
                for _ in range(5):
                    status, _, _ = request("GET", f"{base}/internal-resolve/{api_slug}/health")
                    if status != 200:
                        break
                check(status == 200, "Polling repetido sin max_uses funciona")

        # 5. Revocación
        status, _, _ = request("DELETE", f"{base}/api/links/{file_slug}", token=token)
        check(status == 200, "Revocar enlace file")
        status, _, _ = request("GET", f"{base}/internal-resolve/{file_slug}")
        check(status == 404, "Enlace revocado devuelve 404")
    finally:
        for slug in created:
            try:
                request("DELETE", f"{base}/api/links/{slug}", token=token)
            except Exception:
                pass

    print("\nE2E completado correctamente.")


if __name__ == "__main__":
    main()
