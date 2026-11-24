# custom-links

Microservicio de links temporales con FastAPI + SQLite, pensado para usarlo detrás de Nginx Proxy Manager con `X-Accel-Redirect` para archivos estáticos y, opcionalmente, como proxy dinámico para APIs, polling y servidores MCP.

## Características

- Slugs personalizados o aleatorios
- Expiración por tiempo
- Límite de usos por link (opcional)
- Revocación manual
- Soporte Range/streaming vía Nginx (`mode: "file"`)
- **Modo dinámico (`mode: "api"`)** activable por feature flag (`DYNAMIC_LINKS_ENABLED=true`):
  - Reenvía todos los métodos HTTP (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`) y el body
  - Reenvío de subrutas (`/s/{slug}/ruta/extra` -> `{target_url}/ruta/extra`) y query params
  - Streaming sin buffering (compatible con SSE, long-polling y MCP Streamable HTTP)
  - Inyección de headers hacia el destino (ocultos al inspeccionar el link)
  - CORS configurable por link
  - Allowlist opcional de hosts destino (`DYNAMIC_ALLOWED_HOSTS`)
- Persistencia en SQLite con volumen compartido y migración automática
- Puente MCP para agentes

## Levantar servicio (imagen de Docker Hub)

La imagen se publica automáticamente en Docker Hub como [`urielx16/custom-links`](https://hub.docker.com/r/urielx16/custom-links) (`linux/amd64` y `linux/arm64`). Para desplegar solo necesitas `docker-compose.yml` y `.env`:

1. Copia variables:

```bash
cp .env.example .env
```

2. Ajusta `.env`.
3. Verifica la red Docker compartida con NPM (`SHARED_PROXY_NETWORK`).
4. Inicia:

```bash
docker compose up -d
```

Actualizar a la última versión:

```bash
docker compose pull && docker compose up -d
```

### Desarrollo local (construir desde el código)

Crea un `docker-compose.override.yml` (ignorado por Git; Docker Compose lo carga automáticamente):

```yaml
services:
  custom-links:
    build: .

  custom-links-mcp:
    build: .
```

Y levanta con:

```bash
docker compose up -d --build
```

### Publicación (GitHub Actions)

El workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) ejecuta los tests y, si pasan, construye y publica la imagen:

| Evento | Tags publicados |
| :--- | :--- |
| Push a `main` | `latest` |
| Push de tag `vX.Y.Z` | `X.Y.Z` y `X.Y` |

Requiere los secrets `DOCKER_USERNAME` y `DOCKER_PASSWORD` (access token de Docker Hub con permisos Read & Write) en el repositorio de GitHub.

## Nginx Proxy Manager

1. Crea un Proxy Host para tu dominio.
2. Forward Hostname/IP: `custom-links`
3. Forward Port: `8000`
4. Activa SSL.
5. En **Advanced** pega `nginx/npm-advanced.conf`.

## API interna

Header requerido:

```text
Authorization: Bearer <API_TOKEN>
```

### Crear link de archivo (`mode: "file"`, por defecto)

```http
POST /api/links
Content-Type: application/json

{
  "target_url": "http://10.0.0.5:5000/output/video.mp4",
  "expires_in": 3600,
  "custom_slug": "mi-video",
  "filename": "video-final.mp4",
  "max_uses": 100
}
```

### Crear link dinámico para API o MCP (`mode: "api"`)

Requiere `DYNAMIC_LINKS_ENABLED=true` en `.env`.

```http
POST /api/links
Content-Type: application/json

{
  "target_url": "http://10.0.0.5:8001/mcp",
  "mode": "api",
  "custom_slug": "mi-mcp-test",
  "expires_in": 7200,
  "allowed_methods": ["GET", "POST", "DELETE", "OPTIONS"],
  "forward_subpaths": true,
  "inject_headers": {
    "Authorization": "Bearer token-interno"
  },
  "cors_origins": ["http://localhost:5173"]
}
```

Con el ejemplo anterior:
- `POST https://tu-dominio/s/mi-mcp-test` reenvía el JSON/SSE intacto hacia `http://10.0.0.5:8001/mcp` inyectando el header `Authorization`.
- `GET https://tu-dominio/s/mi-mcp-test/health?v=1` reenvía hacia `http://10.0.0.5:8001/mcp/health?v=1`.
- Sin `max_uses`, las interacciones (polling, MCP, SSE) son ilimitadas hasta que expire o se revoque el enlace.

### Parámetros y Valores por Defecto

| Parámetro | Tipo | Requerido | Default | Descripción |
| :--- | :--- | :---: | :---: | :--- |
| `target_url` | string | **Sí** | - | URL de destino real protegida o interna. |
| `mode` | string | No | `"file"` | `"file"` (descarga/streaming vía Nginx `X-Accel-Redirect`) o `"api"` (reverse proxy dinámico). |
| `expires_in` | integer | No | Configurable (ej. 4 h) | Tiempo de expiración en segundos. **Usa `0` para enlaces permanentes**. Máximo permitido cuando es > 0: `2592000` (30 días). Configurable vía `DEFAULT_EXPIRES_IN`. |
| `max_uses` | integer | No | `null` (ilimitado) | Número máximo de accesos permitidos antes de expirar. En `mode: "api"` se recomienda omitirlo para polling o MCP. |
| `custom_slug` | string | No | `null` (aleatorio) | Slug personalizado (hasta 80 caracteres). Admite caracteres alfanuméricos, guiones, guiones bajos y puntos para extensiones de archivo (ej. `Video.mp4`). Si no se envía, genera un hash aleatorio seguro (~11 caracteres). |
| `filename` | string | No | `null` (inferido) | Solo `mode: "file"`. Nombre sugerido de archivo para header `Content-Disposition` (compatible con acentos/UTF-8 según RFC 5987/6266). |
| `allowed_methods` | string[] | No | `DYNAMIC_DEFAULT_METHODS` | Solo `mode: "api"`. Métodos HTTP permitidos (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`). El resto devuelve `405`. |
| `forward_subpaths` | boolean | No | `true` | Solo `mode: "api"`. Permite reenviar subrutas `/s/{slug}/...` al destino. |
| `inject_headers` | object | No | `null` | Solo `mode: "api"`. Headers que se agregan o sobrescriben hacia el destino. En `GET /api/links/{slug}` sus valores se devuelven enmascarados como `"***"`. |
| `cors_origins` | string[] | No | `null` | Solo `mode: "api"`. Orígenes CORS permitidos para ese link (ej. `["http://localhost:5173"]` o `["*"]`). Si se omite, el CORS lo maneja el destino. |
| `count_preflight` | boolean | No | `false` | Solo `mode: "api"`. Si las peticiones preflight `OPTIONS` incrementan `use_count`. |

### Variables de entorno para modo dinámico

| Variable | Default | Descripción |
| :--- | :--- | :--- |
| `DYNAMIC_LINKS_ENABLED` | `false` | Interruptor global para habilitar `mode: "api"`. |
| `DYNAMIC_ALLOWED_HOSTS` | `""` (cualquiera) | Lista separada por coma de hosts permitidos (ej. `10.0.0.5,api.local:8080,*.internal`). |
| `DYNAMIC_DEFAULT_METHODS` | `GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS` | Métodos permitidos por defecto si no se especifican al crear el link. |
| `DYNAMIC_TIMEOUT_SECONDS` | `30` | Timeout de conexión/escritura hacia el destino (en segundos). |
| `DYNAMIC_READ_TIMEOUT_SECONDS` | `0` | Timeout de lectura en segundos (`0` = sin límite, necesario para SSE / MCP / long-polling). |
| `DYNAMIC_MAX_BODY_BYTES` | `10485760` (10 MB) | Tamaño máximo del body reenviado (`0` = sin límite). Ajusta `client_max_body_size` en Nginx si subes este valor. |

Consultar link:

```http
GET /api/links/{slug}
```

Revocar link:

```http
DELETE /api/links/{slug}
```

### Limpieza de enlaces expirados

1. **Limpieza automática periódica (FastAPI Background Task)**:
   - Viene habilitada por defecto (`AUTO_CLEANUP_ENABLED=true`).
   - Se ejecuta en segundo plano dentro del contenedor cada 24 horas (`CLEANUP_INTERVAL_SECONDS=86400`).
   - También realiza una limpieza inicial unos segundos después de que el servicio arranca.
   - Elimina enlaces expirados por fecha, enlaces que alcanzaron su `max_uses`, y enlaces revocados manualmente.

2. **Limpieza manual vía API**:
   ```http
   POST /api/cleanup
   ```

3. **Cron en el servidor (Opcional)**:
   Si prefieres delegar la limpieza a una tarea `crontab` en el host:
   ```bash
   0 3 * * * curl -s -X POST http://localhost:8000/api/cleanup -H "Authorization: Bearer <TU_API_TOKEN>" > /dev/null
   ```

### CORS (Frontend de gestión)

Para permitir llamadas a `/api/links` desde aplicaciones frontend (React, Vue, Svelte, etc.) en otros dominios o puertos:
- `CORS_ORIGINS`: Lista de orígenes permitidos separados por coma (ej. `http://localhost:3000,https://app.midominio.com`) o `*` para todos.
- `CORS_ALLOW_CREDENTIALS`: `true` o `false` (`true` por defecto). Si `CORS_ORIGINS=*`, el sistema ajusta automáticamente credentials para respetar la especificación de Fetch.

## MCP 

El servidor MCP permite que agentes de IA (Claude Desktop, Cursor, Antigravity, etc.) gestionen links automáticamente.

Expone las herramientas:
- `create_link` (crear enlaces estáticos o dinámicos `mode="api"` con límites y opciones)
- `get_link` (consultar estado, modo y usos)
- `revoke_link` (revocar enlaces)

### Modo Remoto HTTP (`/mcp` Streamable HTTP)

Levanta el servicio MCP en segundo plano:

```bash
docker compose --profile mcp up -d custom-links-mcp
```

Quedará escuchando en `http://localhost:8001/mcp` utilizando la especificación estándar moderna de MCP (Streamable HTTP):

```json
{
  "mcpServers": {
    "custom-links": {
      "url": "http://localhost:8001/mcp"
    }
  }
}
```
