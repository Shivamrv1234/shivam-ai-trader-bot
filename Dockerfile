# Container for Google Cloud Run (instant Telegram replies).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    BOT_STATE=/tmp/bot-state.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY config.json .

# One process (the bot's memory lives in it) with a few threads: updates are still handled one at a
# time (bot/server.py), but a message Telegram sends twice is answered at once instead of waiting.
CMD exec gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 8 --timeout 300 bot.server:app
