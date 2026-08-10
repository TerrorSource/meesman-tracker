FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv

# ---- Systeemdeps ----
# Let op: GEEN `playwright install --with-deps` gebruiken — Playwright 1.46
# probeert op Debian bookworm het Ubuntu-pakket 'ttf-ubuntu-font-family' te
# installeren en faalt. Daarom een handmatige lijst Chromium-runtime-deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    # Chromium runtime deps
    libnss3 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libcups2 \
    libdrm2 \
    libxshmfence1 \
    # Fonts
    fonts-liberation \
    fonts-unifont \
 && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
COPY requirements.txt /srv/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/requirements.txt

# ---- Playwright browser ----
RUN python -m playwright install chromium

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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
