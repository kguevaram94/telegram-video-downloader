FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE_BACKEND=process \
    NOTIFY=false \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8765 \
    DOWNLOAD_DIR=/data/videos \
    DATA_DIR=/app/data \
    SESSION_NAME=/app/data/telegram_session \
    STATE_FILE=/app/data/downloaded.json \
    PROGRESS_FILE=/app/data/downloader.progress \
    MANUAL_QUEUE_FILE=/app/data/manual_download_queue.json \
    LOG_PATH=/app/data/downloader.log

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY downloader.py dashboard.py list_chats.py ./
COPY telegram_downloader ./telegram_downloader
COPY static ./static
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/data /data/videos

EXPOSE 8765

VOLUME ["/app/data", "/data/videos"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["all"]
