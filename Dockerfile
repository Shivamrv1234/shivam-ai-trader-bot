# Container for Google Cloud Run (instant Telegram replies).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    BOT_STATE=/tmp/bot-state.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt flask gunicorn

COPY bot ./bot
COPY config.json .

# One request at a time per container (charts use matplotlib, which isn't thread-safe).
CMD exec gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 1 --timeout 300 bot.server:app
