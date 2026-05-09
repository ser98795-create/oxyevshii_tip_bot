FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY persona.txt ./persona.txt

# Запуск без shell-обёртки, чтобы корректно обрабатывать SIGTERM от docker stop.
CMD ["python", "-m", "bot.main"]
