# TradingView Futures Bot — portable image (works on a DO Droplet or App Platform).
# Build context is the repo root so the `bot/` package is importable.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY bot/requirements.txt ./bot/requirements.txt
RUN pip install -r bot/requirements.txt

# App code.
COPY bot/ ./bot/

# Persistent state lives here — mount a volume at /data in production so the
# SQLite DB + error log survive restarts/redeploys.
RUN mkdir -p /data
ENV BOT_DB_PATH=/data/bot_state.db \
    BOT_ERROR_LOG=/data/error_log.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "bot.app:app", "--host", "0.0.0.0", "--port", "8000"]
