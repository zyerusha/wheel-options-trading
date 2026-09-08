# syntax=docker/dockerfile:1

FROM python:3.12-slim

# Stdlib-only app: no pip install, no build deps.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WHEEL_HOST=0.0.0.0 \
    WHEEL_DATA_DIR=/app/data \
    PORT=8765

WORKDIR /app

# Non-root runtime user, fixed uid/gid so a bind-mounted host dir owned by
# uid 1000 stays writable for uploads + market-data caches.
RUN groupadd --gid 1000 app \
 && useradd  --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin app

# Application code ONLY. Never `COPY . .` -- keeps data/ and sandbox/ out of the image.
COPY wheel/ ./wheel/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /app/data \
 && chown -R app:app /app

USER app

# Documentation only; the real port is $PORT at runtime.
EXPOSE 8765

# Stdlib liveness probe (no curl in slim). Hits GET / (static index.html) --
# cheap, no registry.build(), no network. urlopen raises on non-2xx -> unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/', timeout=4).read()"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
