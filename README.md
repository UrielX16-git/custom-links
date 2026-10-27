# custom-links

Microservicio de links temporales con FastAPI + SQLite, pensado para usarlo detrás de Nginx Proxy Manager con `X-Accel-Redirect`.

## Características

- Slugs personalizados o aleatorios
- Expiración por tiempo
- Límite de usos por link
- Revocación manual
- Soporte Range/streaming vía Nginx
- Persistencia en SQLite con volumen compartido
- Puente MCP para agentes

## Levantar servicio

1. Copia variables:

```bash
cp .env.example .env
```

2. Ajusta `.env`.
3. Verifica la red Docker compartida con NPM (`SHARED_PROXY_NETWORK`).
4. Inicia:

```bash
docker compose up -d --build
```

## Nginx Proxy Manager

1. Crea un Proxy Host para tu dominio de descargas.
2. Forward Hostname/IP: `custom-links`
3. Forward Port: `8000`
4. Activa SSL.
5. En **Advanced** pega `nginx/npm-advanced.conf`.

## API interna

Header requerido:

```text
Authorization: Bearer <API_TOKEN>
```

Crear link:

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

### Parámetros y Valores por Defecto

| Parámetro | Tipo | Requerido | Default | Descripción |
| :--- | :--- | :---: | :---: | :--- |
| `target_url` | string | **Sí** | - | URL de destino real protegida o interna. |
| `expires_in` | integer | No | Configurable (ej. 4 h) | Tiempo de expiración en segundos. **Usa `0` para enlaces permanentes**. Máximo permitido cuando es > 0: `2592000` (30 días). Configurable vía `DEFAULT_EXPIRES_IN`. |
| `max_uses` | integer | No | `null` (ilimitado) | Número máximo de aperturas/descargas permitidas antes de expirar. |
| `custom_slug` | string | No | `null` (aleatorio) | Slug personalizado (hasta 80 caracteres). Si no se envía, genera un hash aleatorio seguro (~11 caracteres). |
| `filename` | string | No | `null` | Nombre sugerido de archivo para header `Content-Disposition`. |

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

### CORS (Frontend)

Para permitir llamadas a `/api/links` desde aplicaciones frontend (React, Vue, Svelte, etc.) en otros dominios o puertos:
- `CORS_ORIGINS`: Lista de orígenes permitidos separados por coma (ej. `http://localhost:3000,https://app.midominio.com`) o `*` para todos.
- `CORS_ALLOW_CREDENTIALS`: `true` o `false` (`true` por defecto). Si `CORS_ORIGINS=*`, el sistema ajusta automáticamente credentials para respetar la especificación de Fetch.

## MCP 

El servidor MCP permite que agentes de IA (Claude Desktop, Cursor, Antigravity, etc.) gestionen links automáticamente.

Expone las herramientas:
- `create_link` (crear enlaces temporales con límites)
- `get_link` (consultar estado y usos)
- `revoke_link` (revocar enlaces)

### Modo Remoto HTTP (`/mcp` Streamable HTTP)

Levanta el servicio MCP en segundo plano:

```bash
docker compose --profile mcp up -d --build custom-links-mcp
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

