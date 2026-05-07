# Slim production image for pump_bot.
# Build:  docker build -t pump_bot .
# Run:    docker compose up -d
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 pump

WORKDIR /app

# Install deps first for build-cache friendliness. playwright is dropped
# from the image because nothing imports it (verified with grep).
COPY requirements.txt ./
RUN sed -i '/^playwright/d' requirements.txt \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs && chown -R pump:pump /app

USER pump

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/status || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "main.py"]
