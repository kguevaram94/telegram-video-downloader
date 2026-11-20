# Telegram Video Downloader

Monitor automático de un grupo privado de Telegram. Pensado para **despliegue exclusivo** en UGREEN NAS DXP2800 (UGOS Pro) con Docker Compose. El contenedor descarga videos y sirve el panel web 24/7.

Detalle de montajes: [docs/telegram-downloader-nas-mounts.md](docs/telegram-downloader-nas-mounts.md).

## Requisitos

- UGREEN NAS DXP2800 / UGOS Pro con Docker Compose (despliegue principal)
- Cuenta de Telegram con acceso al grupo privado
- Credenciales de API (`api_id` y `api_hash`) desde [my.telegram.org](https://my.telegram.org/apps)
- Opcional: Python 3.9+ solo para generar la sesión Telethon o uso local puntual

## Credenciales (`config.env`)

```bash
cp config.env.example /volume1/docker/telegram-downloader/config.env
```

Edita solo secretos y chat (Compose fija rutas de datos y descarga):

```env
API_ID=12345678
API_HASH=tu_api_hash_aqui
CHAT_ID=-1001234567890
```

No pongas claves en `docker-compose.yml`.

### Obtener `api_id` y `api_hash`

1. Entra en [my.telegram.org](https://my.telegram.org) con tu número de Telegram.
2. Ve a **API development tools** → **Create application**.
3. Rellena el formulario (para uso personal, cualquier nombre vale).
4. Copia `App api_id` y `App api_hash` a `config.env`.

Si al crear la app ves `ERROR` con HTTP 200, es un rechazo silencioso de Telegram. Prueba en ventana privada, sin adblock/VPN, o desde datos móviles.

### Obtener el ID del grupo

```bash
python3 list_chats.py
```

La primera vez pedirá teléfono y código de verificación. Busca tu grupo en la lista y copia su ID (suele ser un número negativo) a `CHAT_ID`.


## Despliegue en NAS (Docker) — UGREEN / UGOS Pro

`docker-compose.yml` es **solo NAS**: rutas absolutas UGOS, sin `VIDEO_DIR` ni paths de MacBook.

### 1. Crear carpetas en UGOS Pro

```bash
mkdir -p /volume1/Media/Downloads/Inbox \
         /volume1/Media/Movies \
         /volume1/Media/Series \
         /volume1/Media/Documentaries \
         "/volume1/Media/Home Videos" \
         /volume1/docker/telegram-downloader/data
```

### 2. Configuración y sesión

```bash
cp config.env.example /volume1/docker/telegram-downloader/config.env
# Edita API_ID, API_HASH y CHAT_ID en ese archivo
```

Si ya tienes sesión Telethon / historial (p. ej. generados en otro equipo):

```bash
cp telegram_session.session /volume1/docker/telegram-downloader/data/telegram_session.session
cp downloaded.json /volume1/docker/telegram-downloader/data/downloaded.json   # si aplica
```

Persistencia en `/volume1/docker/telegram-downloader/data`: sesión, `downloaded.json`, cola manual, progreso y logs.

### 3. Correspondencia de montajes (un solo bind)

| Contenedor | NAS |
|------------|-----|
| `/data/media` | `/volume1/Media` |
| `/data/media/Downloads` | `/volume1/Media/Downloads` (`DOWNLOAD_DIR`) |
| `/data/media/Downloads/Inbox` | `/volume1/Media/Downloads/Inbox` |
| `/data/media/Movies` | `/volume1/Media/Movies` |
| `/data/media/Series` | `/volume1/Media/Series` |
| `/app/data` | `/volume1/docker/telegram-downloader/data` |
| `/app/config.env` | `/volume1/docker/telegram-downloader/config.env` |

Un único mount evita `EXDEV` al renombrar entre Inbox y Movies/Series.
Jellyfin sigue usando las rutas físicas `/volume1/Media/Movies` y `/volume1/Media/Series`.

### 4. Arrancar

Desde el directorio del repo (con el `docker-compose.yml` de este proyecto):

```bash
docker compose up -d --build
```

Puerto del panel: `DASHBOARD_PORT` (por defecto `8765`). Ejemplo:

```bash
DASHBOARD_PORT=8765 docker compose up -d --build
```

Panel: `http://IP_DEL_NAS:8765`

Comprobar logs:

```bash
docker compose logs -f
# o
tail -f /volume1/docker/telegram-downloader/data/downloader.log
```

### 5. Operación habitual

| Acción | Comando |
|--------|---------|
| Ver estado | `docker compose ps` |
| Parar | `docker compose stop` |
| Arrancar | `docker compose start` |
| Reiniciar | `docker compose restart` |
| Actualizar código | `docker compose up -d --build` |

Desde el panel web también puedes iniciar/detener/reiniciar el descargador (modo `process`).

### Notas NAS

- El panel escucha en `0.0.0.0` dentro del contenedor (accesible en la LAN).
- Las notificaciones macOS quedan desactivadas (`NOTIFY=false`).
- No expongas el puerto 8765 a Internet sin protección (VPN o reverse proxy con auth).
- Una sola sesión Telethon activa por archivo `.session` (no correr Mac + NAS a la vez con la misma sesión).
- Docker local de desarrollo (rutas relativas): ver `docker-compose.local.example.yml`.
- Tests: `python3 -m pytest tests/ -q`

## Uso manual

```bash
python3 downloader.py
```

El script queda escuchando el grupo. Reenvía un video y se descargará solo.

### Backfill (videos ya existentes en el grupo)

```bash
python3 downloader.py --backfill 50
```

Revisa los últimos 50 mensajes, encola los videos encontrados y los descarga uno a uno. Usa la misma cola y el mismo progreso que los videos nuevos.

Los mensajes ya registrados en `downloaded.json` no se vuelven a descargar.

## Nombres de archivo

Prioridad al guardar cada video:

1. **Texto/leyenda del mensaje** en Telegram → p. ej. `Naruto - Capitulo 01.mp4`
2. Nombre original del archivo en Telegram
3. Nombre genérico con fecha → `video_2026-06-08_170937_2125.mp4`

Para que funcione con el nombre del capítulo, añade o conserva la **leyenda** al reenviar el video al grupo.

Detalles:

- Si la leyenda no lleva extensión, se añade `.mp4`, `.mkv`, etc. según el archivo.
- Caracteres no válidos (`/`, `:`, etc.) se sustituyen por `_`.
- Si dos capítulos tienen el mismo nombre, al segundo se le añade el ID del mensaje.

En NAS, `DOWNLOAD_DIR` es `/data/media/Downloads` (= `/volume1/Media/Downloads`).
Layout bajo el mount único `/data/media`:

```
/data/media/
  Downloads/
    Inbox/          ← aterrizaje de descargas
  Movies/           ← hermano de Downloads
  Series/
  Documentaries/
  Home Videos/
```

## Progreso y cola

### Progreso en una sola línea

En terminal interactiva, el progreso se actualiza en la misma línea:

```
[+2 en cola] Naruto - Capitulo 03.mp4:  45% (120.5 MB / 267.0 MB)
```

Al terminar:

```
Completado: Naruto - Capitulo 03.mp4 (267.0 MB)
```

Si el script escribe a un log (arranque automático), cada actualización va en líneas separadas porque los archivos de log no soportan `\r`.

### Cola de descargas

- **Sin límite** de tamaño de cola.
- Los videos se descargan **uno a uno**, en orden de llegada.
- Si llegan varios seguidos, verás `[+N en cola]` con cuántos esperan.
- El mismo mensaje no se encola dos veces.
- Backfill y videos nuevos comparten la misma cola.

Límites prácticos: espacio en disco y velocidad de la API de Telegram (suele ser más lenta que la app oficial).

## Panel de estado (UI local)

Hay un panel web local para ver el estado del servicio, leer logs en tiempo real y controlar el descargador.

### Instalar dependencias del panel

```bash
pip3 install -r requirements.txt --user
```

### Arrancar el panel manualmente

```bash
python3 dashboard.py
```

Abre en el navegador: [http://127.0.0.1:8765](http://127.0.0.1:8765)

Solo escucha en `127.0.0.1`, así que no es accesible desde otros dispositivos de la red.

### Acceso rápido desde el sistema

Incluye una app de macOS:

```text
Telegram Downloader.app
```

Haz doble clic para abrir el panel en el navegador. Puedes arrastrarla a **Aplicaciones** o al Dock.

### Arranque automático del panel

```bash
cp /Users/kevin/Projectos/Mio/telegram-video-downloader/com.telegram.downloader-dashboard.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.telegram.downloader-dashboard.plist
```

Log del panel:

```bash
tail -f /Users/kevin/Projectos/Mio/telegram-video-downloader/dashboard.log
```

### Qué puedes hacer en el panel

- Ver si el descargador está **activo** o detenido
- Ver PID, LaunchAgent y carpeta de descargas
- Leer **logs en vivo**
- **Iniciar**, **detener** o **reiniciar** el servicio
- **Abrir la carpeta** de descargas en Finder

## Arranque automático al encender el Mac

El proyecto incluye un **LaunchAgent** para macOS. Se inicia al **iniciar sesión** (no antes de la pantalla de login) y se reinicia solo si falla.

### Instalar

```bash
cp /Users/kevin/Projectos/Mio/telegram-video-downloader/com.telegram.video-downloader.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.telegram.video-downloader.plist
```

### Comprobar que está corriendo

Ver la sección [Comprobar si está en background](#comprobar-si-está-en-background).

### Ver log en vivo

```bash
tail -f /Users/kevin/Projectos/Mio/telegram-video-downloader/downloader.log
```

### Parar el servicio

```bash
launchctl bootout gui/$(id -u)/com.telegram.video-downloader
```

### Arrancar manualmente

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.telegram.video-downloader.plist
```

### Reiniciar tras cambiar script o `config.env`

```bash
launchctl kickstart -k gui/$(id -u)/com.telegram.video-downloader
```

### Desinstalar arranque automático

```bash
launchctl bootout gui/$(id -u)/com.telegram.video-downloader
rm ~/Library/LaunchAgents/com.telegram.video-downloader.plist
```

## Comprobar si está en background

### 1. Servicio de macOS (LaunchAgent)

```bash
launchctl list | grep telegram
```

Si aparece `com.telegram.video-downloader` con un **número a la izquierda** (PID, por ejemplo `88730`), el servicio está activo.

Si la columna del PID está vacía o el exit code no es `0`, no está corriendo.

### 2. Proceso de Python

```bash
pgrep -fl downloader.py
```

Si devuelve una línea con `downloader.py`, el script está en ejecución.

### 3. Log del servicio

```bash
tail -f /Users/kevin/Projectos/Mio/telegram-video-downloader/downloader.log
```

Si ves algo como:

```
Monitoreando: Watchlist
Carpeta destino: /Users/kevin/Downloads/Watchlist
Esperando nuevos videos...
```

está escuchando el grupo. Si descarga algo, aparecerán líneas de progreso o `Completado: ...`.

Pulsa `Ctrl+C` para salir de `tail -f`.

### 4. Prueba real

Reenvía un video al grupo y luego revisa:

```bash
tail -20 /Users/kevin/Projectos/Mio/telegram-video-downloader/downloader.log
```

O comprueba si aparece un archivo nuevo en la carpeta configurada en `DOWNLOAD_DIR`.

### Background vs terminal manual

| Cómo lo arrancaste | Cómo saberlo |
|--------------------|--------------|
| **LaunchAgent** (arranque automático) | `launchctl list \| grep telegram` muestra el servicio |
| **Terminal manual** (`python3 downloader.py`) | `pgrep -fl downloader.py` lo muestra; al cerrar esa terminal se detiene |

Con el LaunchAgent instalado, el script sigue corriendo aunque cierres Cursor, iTerm u otras apps.

### Si no está corriendo

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.telegram.video-downloader.plist
```

## Archivos del proyecto

| Archivo | Descripción |
|---------|-------------|
| `downloader.py` | Punto de entrada del monitor y descargador |
| `dashboard.py` | Punto de entrada del panel web local |
| `telegram_downloader/` | Paquete con la lógica (config, cola, nombres, panel) |
| `static/index.html` | Interfaz del panel |
| `Telegram Downloader.app` | Acceso rápido al panel desde macOS |
| `list_chats.py` | Lista chats para obtener `CHAT_ID` |
| `config.env` | Configuración (no compartir) |
| `downloaded.json` | IDs de mensajes ya descargados |
| `telegram_session.session` | Sesión de Telegram (sensible) |
| `downloader.log` | Log cuando corre con LaunchAgent |
| `dashboard.log` | Log del panel web |
| `com.telegram.video-downloader.plist` | Servicio de arranque automático |
| `com.telegram.downloader-dashboard.plist` | Arranque automático del panel |
| `Dockerfile` / `docker-compose.yml` | Despliegue 24/7 en UGREEN NAS (un bind `/volume1/Media`) |
| `docker-compose.local.example.yml` | Ejemplo Docker local con rutas relativas |
| `docs/telegram-downloader-nas-mounts.md` | Tabla de montajes y carpetas previas en UGOS |
| `telegram_downloader/media_classifier.py` | Helpers de path + `move_without_overwrite` (sin TMDb) |
| `tests/` | Tests de paths, Inbox y EXDEV |
| `/volume1/docker/telegram-downloader/data` | Sesión, estado, cola, progreso, logs (en el NAS) |

## Seguridad

- No subas `config.env`, `telegram_session.session` ni `downloaded.json` a repositorios públicos.
- `api_id` / `api_hash` identifican la aplicación; la sesión equivale a estar logueado en tu cuenta.
- Usa el script solo con contenido al que tengas acceso y respetando los términos de Telegram.

## Flujo recomendado (NAS)

1. Crea las carpetas UGOS listadas arriba.
2. Crea la app en my.telegram.org y configura `/volume1/docker/telegram-downloader/config.env`.
3. Genera sesión (`list_chats.py` u otro equipo) y cópiala a `.../data/telegram_session.session`.
4. Anota el `CHAT_ID` en `config.env`.
5. `docker compose up -d --build` y abre `http://IP_DEL_NAS:8765`.
6. Reenvía un video de prueba al grupo y verifica Inbox / clasificación.

## Uso local (opcional)

Para desarrollo local sin NAS, puedes usar Python directamente o
`docker-compose.local.example.yml` (rutas relativas `./data` y `./library`).
Las secciones de LaunchAgent / app macOS más abajo son opcionales y no forman
parte del despliegue NAS.
