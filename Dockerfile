FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv

# ---- Basis systeemdeps ----
# tini = mini-init als PID 1 (ruimt verweesde Chromium-processen op)
# gosu = privileges laten vallen naar de app-gebruiker in de entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    tini \
    gosu \
 && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
COPY requirements.txt /srv/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/requirements.txt

# ---- Playwright: alleen de lichte chromium-headless-shell ----
RUN apt-get update \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright

# ---- App-versie (door CI als build-arg meegegeven; lokaal 'dev') ----
ARG APP_VERSION=dev
ARG APP_COMMIT=""
ENV APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT}

# ---- Non-root gebruiker (uid/gid worden bij de start op PUID/PGID gezet) ----
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -d /home/app -s /usr/sbin/nologin app

# ---- App code ----
COPY app /srv/app
COPY docker-entrypoint.sh /docker-entrypoint.sh
# Code wereld-leesbaar (PUID kan de uid van 'app' bij de start wijzigen,
# dus niet op eigenaarschap vertrouwen); alleen lezen is genoeg
RUN chmod +x /docker-entrypoint.sh && chmod -R a+rX /srv

# ---- Data dir ----
RUN mkdir -p /data && chown app:app /data

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
