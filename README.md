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

Consultar link:

```http
GET /api/links/{slug}
```

Revocar link:

```http
DELETE /api/links/{slug}
```

Limpieza manual:

```http
POST /api/cleanup
```

## MCP 

El servidor MCP permite que agentes de IA (Claude Desktop, Cursor, Antigravity, etc.) gestionen links automáticamente.

Expone las herramientas:
- `create_link` (crear enlaces temporales con límites)
- `get_link` (consultar estado y usos)
- `revoke_link` (revocar enlaces)

## Modo Remoto SSE 

Levanta el servicio MCP en segundo plano:

```bash
docker compose --profile mcp up -d custom-links-mcp
```

Quedará escuchando en `http://localhost:8001/sse`. En clientes compatibles con SSE (Cursor, LibreChat, etc.), añade:

```json
{
  "mcpServers": {
    "custom-links": {
      "url": "http://localhost:8001/sse"
    }
  }
}
```

