# Telegram Downloader — montajes NAS (UGREEN DXP2800 / UGOS Pro)

Despliegue exclusivo en NAS. `docker-compose.yml` usa **un solo bind mount** del
árbol de media para que `Path.replace()` no cruce dispositivos (evita EXDEV).

No contiene secretos: credenciales van en `config.env`.

## Carpetas a crear antes en UGOS Pro

```text
/volume1/Media
/volume1/Media/Downloads
/volume1/Media/Downloads/Inbox
/volume1/Media/Movies
/volume1/Media/Series
/volume1/Media/Documentaries
/volume1/Media/Home Videos
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
| `/data/media` | `/volume1/Media` | Biblioteca completa (único bind mount) |
| `/data/media/Downloads` | `/volume1/Media/Downloads` | `DOWNLOAD_DIR` |
| `/data/media/Downloads/Inbox` | `/volume1/Media/Downloads/Inbox` | Inbox de descargas |
| `/data/media/Movies` | `/volume1/Media/Movies` | Películas (hermano de Downloads) |
| `/data/media/Series` | `/volume1/Media/Series` | Series (hermano de Downloads) |
| `/data/media/Documentaries` | `/volume1/Media/Documentaries` | Documentales |
| `/data/media/Home Videos` | `/volume1/Media/Home Videos` | Home videos |
| `/app/data` | `/volume1/docker/telegram-downloader/data` | Sesión, historial, cola, progreso, logs |
| `/app/config.env` | `/volume1/docker/telegram-downloader/config.env` | Credenciales (solo lectura) |

Puerto dashboard: host `${DASHBOARD_PORT:-8765}` → contenedor `8765`.

Compose fija:

```text
DOWNLOAD_DIR=/data/media/Downloads
```

Jellyfin sigue apuntando a las rutas físicas del NAS
(`/volume1/Media/Movies`, `/volume1/Media/Series`, …) sin cambios.

## Por qué un solo mount (anti-EXDEV)

Montajes separados (`Downloads`, `Movies`, `Series` en binds distintos) hacen que
`Path.replace()` / `os.rename` fallen con `EXDEV` (Invalid cross-device link)
al mover desde Inbox hacia Movies/Series.

Con **un** bind `/volume1/Media` → `/data/media`, Inbox y las bibliotecas son
hermanos en el mismo filesystem del contenedor; el rename atómico funciona.

## Clasificación TMDb

`telegram_downloader/media_classifier.py` incluye:

- Helpers de rutas (`media_root_from_download_dir`, `inbox_dir`, `movies_dir`,
  `series_dir`) y `move_without_overwrite` (rename + fallback EXDEV).
- Cliente async TMDb (`AsyncTmdbHttpClient`), caché (`PersistentJsonCache` /
  `CachingTmdbClient`) y `MediaClassifier` con umbral/ambigüedad.

Tras completar la descarga (`.part` → archivo final), las coincidencias van a:

- Películas: `/data/media/Movies/Título (Año)/…`
- Series: `/data/media/Series/Título/Season NN/…`

Si falla TMDb, hay ambigüedad o no hay confianza suficiente, el archivo permanece
en `Downloads/Inbox`.

## Persistencia en `/app/data`

| Archivo / uso | Path típico en contenedor |
|---------------|---------------------------|
| Sesión Telethon | `/app/data/telegram_session.session` |
| Historial de descargas | `/app/data/downloaded.json` |
| Cola manual | `/app/data/manual_download_queue.json` |
| Progreso | `/app/data/downloader.progress` |
| Log | `/app/data/downloader.log` |
| Caché TMDb | `/app/data/tmdb_cache.json` |

## Qué no hacer

- No usar varios bind mounts para Movies/Series separados del árbol Media.
- No poner `API_ID` / `API_HASH` / tokens en YAML.
- No desplegar ni lanzar descargas reales solo con esta documentación.
- No modificar ni borrar archivos del NAS desde el agente de desarrollo.
