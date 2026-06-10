FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv

# ---- Basis systeemdeps (curl voor de healthcheck) ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
 && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
COPY requirements.txt /srv/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/requirements.txt

# ---- Playwright browser + bijbehorende systeemdeps ----
# --with-deps installeert precies de libraries die déze Playwright-versie
# nodig heeft (vervangt de handmatige apt-lijst van vroeger)
RUN python -m playwright install --with-deps chromium \
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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
