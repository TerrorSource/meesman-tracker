FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv

# ---- Basis systeemdeps ----
# tini = mini-init als PID 1: ruimt verweesde Chromium-processen (zombies)
# op. Zonder tini stapelen die zich op tot Chromium niet meer kan starten.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    tini \
 && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
COPY requirements.txt /srv/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/requirements.txt

# ---- Playwright: alleen de lichte chromium-headless-shell ----
# --only-shell: geen volledige Chromium (scheelt ~150 MB en geheugen op de NAS)
# --with-deps:  laat Playwright zelf de juiste systeemlibraries installeren
#               (werkt vanaf Playwright ≥1.49 correct op Debian bookworm)
RUN apt-get update \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

# ---- App-versie (door CI als build-arg meegegeven; lokaal 'dev') ----
ARG APP_VERSION=dev
ARG APP_COMMIT=""
ENV APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT}

# ---- App code ----
COPY app /srv/app

# ---- Data dir ----
RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
