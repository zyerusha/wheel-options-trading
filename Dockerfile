# syntax=docker/dockerfile:1

FROM python:3.12-alpine

# Stdlib-only app: no pip install, no build deps. Alpine over slim (Debian)
# for a much smaller CVE surface -- same Python 3.12, just a musl/busybox
# base instead of glibc/Debian (`docker scout quickview` went from 5
# critical/10 high on 3.12-slim to 0/0 here, after the apk upgrade below).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WHEEL_HOST=0.0.0.0 \
    WHEEL_DATA_DIR=/app/data \
    PORT=8765

WORKDIR /app

# Pick up OS-package security patches released after this base image was
# built (e.g. util-linux/libuuid CVEs) -- `--no-cache` fetches the index
# on the fly instead of leaving it in the image layer.
RUN apk upgrade --no-cache

# The base image bundles pip for `pip install`-based images; this one never
# runs pip (stdlib-only, nothing to install), so it's dead weight that only
# adds pip's own CVEs to every scan. Delete its package dir + console
# scripts rather than `pip uninstall`, which can't reliably remove itself.
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
 && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

# Non-root runtime user, fixed uid/gid so a bind-mounted host dir owned by
# uid 1000 stays writable for uploads + market-data caches. Alpine's busybox
# ships addgroup/adduser, not Debian's groupadd/useradd.
RUN addgroup -g 1000 app \
 && adduser -D -H -u 1000 -G app -h /app -s /sbin/nologin app

# Application code ONLY. Never `COPY . .` -- keeps data/ and sandbox/ out of the image.
COPY wheel/ ./wheel/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /app/data \
 && chown -R app:app /app

USER app

# Documentation only; the real port is $PORT at runtime.
EXPOSE 8765

# Stdlib liveness probe (no curl on this base image). Hits GET / (static index.html) --
# cheap, no registry.build(), no network. urlopen raises on non-2xx -> unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/', timeout=4).read()"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
