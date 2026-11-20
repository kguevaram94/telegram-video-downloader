# Telegram Downloader — montajes NAS (UGREEN DXP2800 / UGOS Pro)

Despliegue exclusivo en NAS. `docker-compose.yml` usa rutas absolutas UGOS.
No contiene secretos: credenciales van en `config.env`.

## Carpetas a crear antes en UGOS Pro

```text
/volume1/Media/Downloads
/volume1/Media/Downloads/Inbox
/volume1/Media/Movies
/volume1/Media/Series
/volume1/docker/telegram-downloader
/volume1/docker/telegram-downloader/data
```

Coloca `config.env` en:

```text
/volume1/docker/telegram-downloader/config.env
```

(usa `config.env.example` como plantilla; no subas claves al repo).

## Correspondencia contenedor ↔ NAS

| Ruta en contenedor | Ruta en NAS | Rol |
|--------------------|-------------|-----|
| `/data/videos` | `/volume1/Media/Downloads` | Raíz `DOWNLOAD_DIR` |
| `/data/videos/Inbox` | `/volume1/Media/Downloads/Inbox` | Inbox de descargas |
| `/data/videos/Media/Movies` | `/volume1/Media/Movies` | Destino películas (overlay) |
| `/data/videos/Media/Series` | `/volume1/Media/Series` | Destino series (overlay) |
| `/app/data` | `/volume1/docker/telegram-downloader/data` | Sesión, historial, cola, progreso, logs, caché TMDb |
| `/app/config.env` | `/volume1/docker/telegram-downloader/config.env` | Credenciales (solo lectura) |

Puerto dashboard: host `${DASHBOARD_PORT:-8765}` → contenedor `8765`.

## Por qué overlays (sin duplicar rutas)

Si solo montaras `Downloads` en `/data/videos`, el clasificador al escribir en
`DOWNLOAD_DIR/Media/Movies` crearía:

```text
/volume1/Media/Downloads/Media/Movies   ← duplicado / anidado (incorrecto)
```

en lugar de la biblioteca real `/volume1/Media/Movies`.

Compose monta primero `Downloads` → `/data/videos` y luego **sobremonta**:

- `/volume1/Media/Movies` → `/data/videos/Media/Movies`
- `/volume1/Media/Series` → `/data/videos/Media/Series`

Así `DOWNLOAD_DIR/Media/Movies|Series` (estructura que espera el clasificador)
apunta a las carpetas finales del NAS **sin** anidar `Media/` bajo `Downloads`.

Docker puede crear directorios stub vacíos bajo
`/volume1/Media/Downloads/Media/{Movies,Series}` al arrancar los overlays;
quedan enmascarados por los bind mounts y no son el destino real de los archivos.

## Persistencia en `/app/data`

| Archivo / uso | Path típico en contenedor |
|---------------|---------------------------|
| Sesión Telethon | `/app/data/telegram_session.session` |
| Historial de descargas | `/app/data/downloaded.json` |
| Cola manual | `/app/data/manual_download_queue.json` |
| Progreso | `/app/data/downloader.progress` |
| Log | `/app/data/downloader.log` |
| Caché TMDb (si existe) | bajo `/app/data/` |

## Qué no hacer

- No desplegar ni lanzar descargas reales desde esta documentación sola.
- No poner `API_ID` / `API_HASH` / tokens en YAML.
- No cambiar la lógica Python del clasificador para “arreglar” rutas: los mounts bastan.
- No montar solo el árbol `Downloads` encima de Movies/Series sin overlays.
