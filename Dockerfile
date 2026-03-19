FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv

# ---- System deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    wget \
    gnupg \
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

# ---- App code ----
COPY app /srv/app

# ---- Data dir ----
RUN mkdir -p /data

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
