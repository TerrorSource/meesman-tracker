# Meesman Tracker

A self-hosted Docker application that automatically logs into [mijn.meesman.nl](https://mijn.meesman.nl), scrapes account balances and stores them in a local database. Features a dashboard, REST API for Home Assistant, and Telegram notifications on balance changes.

![Python](https://img.shields.io/badge/python-3.11-blue) ![Docker](https://img.shields.io/badge/docker-compose-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Automatic balance scraping** via Playwright (chromium-headless-shell)
- **TOTP support** — fully unattended, no manual MFA codes needed
- **Scheduled refresh at a fixed local time** (default 07:30, optionally Mon–Sat or Mon–Fri) with catch-up after a (re)start and two automatic retries (30 and 90 min) after a failed scrape; interval mode as fallback
- **Self-healing** — retries with a fresh browser context when a stale session blocks the login; restarts the container when Chromium itself can no longer start
- **Session keepalive** for manual-MFA mode (HTTP cookie check, full browser only every Nth tick); skipped entirely in TOTP mode
- **Dashboard** with charts (1m / 3m / 1y / all), balance history, return per account, a configurable growth baseline date and a **return-per-calendar-year** table; individual data points can be deleted from the changes table; dark mode follows your system setting
- **Account archiving** — closed accounts disappear from the dashboard, totals, API and notifications while their history is kept; you get a Telegram notice when a known account no longer appears at Meesman
- **Deposit tracking** — add, edit and delete deposits; distinguish your own deposits from actual investment returns
- **Home Assistant REST API** — `/api/sensors` and `/deposits.json`
- **CSV export** — `/export.csv` and `/deposits.csv` (Dutch Excel format)
- **Telegram notifications** on balance change (with optional € / % thresholds), session expiry, repeated refresh failures (with the debug screenshot attached, incl. recovery), browser failures, missing accounts, new messages in your Meesman inbox, and monthly/weekly summaries
- **Import** of historical `export.json` and `deposits.json` files
- **Manual data points** — add historical balances for any date
- **Status page** with the last 30 refreshes (time, status, duration, message) and a **health endpoint** (`/health`) with `ok`/`degraded` status; `/api/sensors` carries the same freshness fields for Home Assistant
- **Daily database backups** (`data/backups/`, consistent copies via `VACUUM INTO`, configurable retention)
- **Scraper selectors editable in the UI** — fix a Meesman DOM change without waiting for a new release
- **Selector canary** — a daily GitHub Action checks whether the Meesman login page still matches the scraper's selectors
- **Automatic housekeeping** — log tables (90 days) and debug dumps (30 days) are pruned at startup

---

## Deployment

### Docker Compose (recommended)

No build required — the image is pulled automatically from GitHub Container Registry.

```bash
# Create a data directory
mkdir -p meesman-tracker/data

# Download the compose file
curl -o meesman-tracker/docker-compose.yml \
  https://raw.githubusercontent.com/TerrorSource/meesman-tracker/main/docker-compose.yml

# Start the container
cd meesman-tracker
docker compose up -d
```

Open `http://localhost:8080/config` to complete setup.

### Portainer

1. In Portainer, go to **Stacks** → **Add stack**
2. Name the stack `meesman-tracker`
3. Paste the following into the **Web editor**:

```yaml
services:
  meesman-tracker:
    # Registry names must be lowercase
    image: ghcr.io/terrorsource/meesman-tracker:latest
    container_name: meesman-tracker
    ports:
      - "8080:8080"
    environment:
      - TZ=Europe/Amsterdam
      - DB_PATH=/data/app.db
      - CONFIG_PATH=/data/config.yaml
    volumes:
      - /opt/meesman-tracker/data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
```

> **Note:** Replace `/opt/meesman-tracker/data` with an absolute path on your host. Make sure the directory exists before deploying:
> ```bash
> mkdir -p /opt/meesman-tracker/data
> ```

4. Click **Deploy the stack**

### Updating

```bash
docker compose pull
docker compose up -d
```

### Building from source

If you want to build the image locally instead of pulling it:

```bash
git clone https://github.com/TerrorSource/meesman-tracker.git
cd meesman-tracker
docker compose -f docker-compose.build.yml up -d --build
```

---

## First-time setup

After the container is running, open `http://<host>:8080/config`:

1. **Generate master key** — click *Generate key*. This creates a Fernet encryption key stored in `config.yaml`.
2. **Enter credentials** — your Meesman username and password.
3. **Configure MFA** — choose one method:
   - **TOTP (recommended):** Find the base32 secret in your authenticator app under "manual entry". Enter it in the *TOTP secret* field. The app will generate codes automatically from now on.
   - **Manual code:** Enter a fresh code just before clicking Save, then immediately click *↻ Refresh now*.
4. **Telegram** (optional) — enter your bot token and chat ID to receive balance change notifications.
5. **Planning** — the daily refresh time defaults to 07:30 local time (Meesman updates prices once per trading day). Clear it to fall back to an interval in hours.
6. Click **Save** → **↻ Refresh now**

---

## Upgrading to a new version

Keep the contents of your `data/` volume — that is all you need:

| File | Contents |
|---|---|
| `data/config.yaml` | Credentials, TOTP secret, Telegram config (encrypted) |
| `data/export.json` | Full balance history |
| `data/deposits.json` | All deposit records |
| `data/session.json` | Playwright session state (optional, speeds up first login) |
| `data/cookies.json` | Cookie metadata for the session page (optional) |
| `data/app.db` (+ `app.db-wal`, `app.db-shm`) | SQLite database. It runs in WAL mode: when backing up a *running* container, copy all three files together, or stop the container first |
| `data/backups/` | Daily consistent database copies (`app-YYYYMMDD-HHMMSS.db`, made with `VACUUM INTO`) — the easiest thing to restore from: stop the container, copy one over `data/app.db` (remove the `-wal`/`-shm` files), start again |

**Steps (prebuilt image from ghcr.io — default `docker-compose.yml`):**
```bash
docker compose pull
docker compose up -d
```

**Steps (building from source — `docker-compose.build.yml`):**
```bash
docker compose -f docker-compose.build.yml down
# Replace application files, keeping your data/ directory intact
docker compose -f docker-compose.build.yml up -d --build
```

On first startup after an upgrade, the app automatically restores deposits from `deposits.json` if the table is empty. Import `export.json` via `/import` to restore balance history.

---

## Home Assistant integration

### Total portfolio value

```yaml
# configuration.yaml
sensor:
  - platform: rest
    resource: http://192.168.1.x:8080/api/sensors
    name: Meesman Total
    value_template: "{{ value_json.total }}"
    unit_of_measurement: EUR
    device_class: monetary
    scan_interval: 3600
    json_attributes:
      - accounts
```

### Per account (via template sensor)

```yaml
template:
  - sensor:
      - name: Meesman Investments
        state: >
          {{ state_attr('sensor.meesman_total', 'accounts')
             | selectattr('account_number', 'eq', '12345678')
             | map(attribute='value_eur') | first | round(2) }}
        unit_of_measurement: EUR
        device_class: monetary

      - name: Meesman Pension
        state: >
          {{ state_attr('sensor.meesman_total', 'accounts')
             | selectattr('account_number', 'eq', '87654321')
             | map(attribute='value_eur') | first | round(2) }}
        unit_of_measurement: EUR
        device_class: monetary
```

### Direct per account endpoint

```yaml
sensor:
  - platform: rest
    resource: http://192.168.1.x:8080/api/sensors/12345678
    name: Meesman Investments
    value_template: "{{ value_json.value_eur }}"
    unit_of_measurement: EUR
    device_class: monetary
    scan_interval: 3600
```

### Total deposits

```yaml
sensor:
  - platform: rest
    resource: http://192.168.1.x:8080/deposits.json
    name: Meesman Total Deposits
    value_template: >
      {{ value_json.deposits | map(attribute='amount_eur') | sum | round(2) }}
    unit_of_measurement: EUR
    device_class: monetary
    scan_interval: 86400
```

### Data freshness (alerting when the tracker stalls)

`/api/sensors` also returns `status` (`ok`/`degraded`), `last_ok_refresh` and `last_ok_age_hours`:

```yaml
template:
  - binary_sensor:
      - name: Meesman data stale
        state: "{{ state_attr('sensor.meesman_total', 'status') != 'ok' }}"
        device_class: problem
```

(add `status`, `last_ok_refresh` and `last_ok_age_hours` to `json_attributes` of the REST sensor above)

---

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/sensors` | GET | All accounts + total (HA-friendly JSON) |
| `/api/sensors/{account_number}` | GET | Single account balance |
| `/api/accounts` | GET | Known account numbers and labels |
| `/export.json` | GET | Full balance history |
| `/export.csv` | GET | Balance history as CSV (Dutch Excel format) |
| `/deposits.json` | GET | All deposit records |
| `/deposits.csv` | GET | Deposits as CSV (Dutch Excel format) |
| `/health` | GET | Healthcheck with app version |
| `/refresh-now` | POST | Trigger manual refresh |
| `/datapoints/delete` | POST | Delete a single balance data point (`account_number` + `ts`) |
| `/accounts/{account_number}/archive` | POST | Archive an account (hidden from dashboard, totals, API, notifications) |
| `/accounts/{account_number}/unarchive` | POST | Restore an archived account |
| `/config/selectors/reset` | POST | Reset the scraper selectors to the defaults |
| `/import` | GET/POST | Import `export.json` files |
| `/import/deposits` | POST | Import `deposits.json` |
| `/import/manual` | POST | Add a single manual data point |
| `/deposits` | GET | Deposit management page |
| `/deposits/add` | POST | Add a deposit |
| `/deposits/update/{id}` | POST | Edit a deposit |
| `/deposits/delete/{id}` | POST | Delete a deposit |
| `/session` | GET | Session and cookie status |
| `/config` | GET/POST | Configuration page |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TZ` | `UTC` | Timezone (e.g. `Europe/Amsterdam`), also used for dates in Telegram messages |
| `DB_PATH` | `/data/app.db` | SQLite database path |
| `CONFIG_PATH` | `/data/config.yaml` | Configuration file path |
| `DATA_DIR` | `/data` | Base data directory |
| `EXPORT_PATH` | `/data/export.json` | Balance history export |
| `DEPOSITS_PATH` | `/data/deposits.json` | Deposits export |
| `SESSION_STATE_PATH` | `/data/session.json` | Playwright session state |
| `COOKIES_DUMP_PATH` | `/data/cookies.json` | Cookie dump for the session page |
| `DEBUG_DIR` | `/data/debug` | Screenshots on scrape failure |
| `BACKUP_DIR` | `/data/backups` | Daily database copies |
| `APP_VERSION` / `APP_COMMIT` | `dev` / empty | Set by CI as build args; shown in the footer and `/health` |
| `SELF_RESTART` | `1` | When Chromium cannot start (host problem), exit the process so Docker restarts the container cleanly. Set to `0` to disable |
| `API_CAPTURE` | `0` | Set to `1` to write every JSON response from `*.meesman.nl` during a refresh to `data/debug/api_capture.json` (debugging only) |

---

## Telegram notifications

The app sends a message automatically on:

- **Balance change** — per account with previous/current value, delta and percentage
- **Session expired** — only when using manual MFA; with TOTP the app re-logins automatically
- **Repeated refresh failures** — one warning after 3 consecutive failed refreshes, and a recovery message once refreshing works again
- **Browser failure** — immediately, when Chromium itself cannot start (a host problem such as memory pressure); the container then restarts itself
- **Monthly summary** (on by default) — on the 1st of each month at 08:00: total value, deposits and return for the previous month per account, plus the year-to-date return after deposits
- **Weekly summary** (optional) — Monday 08:00, same layout for the previous 7 days
- **Missing account** — once, when a known (non-archived) account no longer appears in the Meesman overview
- **Meesman inbox** (on by default) — when a new message appears in your inbox on mijn.meesman.nl (dividend payout, annual statement, …). The overview page loads the inbox itself; the tracker picks it up during the refresh. The first refresh silently records the existing inbox. The last 10 messages are listed on the status page.

Thresholds: under *Config → Meldingen* you can require a minimum total change in € and/or % before a balance message is sent (a new account is always announced), and set after how many consecutive failed refreshes the warning goes out.

Example message:
```
📊 Meesman saldo update — 19-03-2026

📈 Beleggingen (12345678)
   Was: € 20.000,00
   Nu:  € 20.500,00 (+2,50%)
   Δ:   +€ 500,00

💰 Totaal: € 45.500,00 (+€ 500,00, +0,26%)
```

---

## Development, tests and CI

- **Tests & lint:** `pip install -r requirements-dev.txt && ruff check app tests scripts && pytest -q tests/` — parser/formatting unit tests, smoke tests that boot the app with a temporary data directory and exercise every route, and an offline regression test of the account-table parser against `tests/fixtures/meesman_overview.html` (needs `playwright install --only-shell chromium`; skipped otherwise). Replace the fixture with an anonymised copy of your own `data/debug/step3_home.html` for maximum realism. The same suite runs in GitHub Actions on every pull request (so Dependabot PRs show a green or red merge button) and before every image build.
- **Selector canary:** `.github/workflows/selector-canary.yml` opens the Meesman login page daily and fails (→ GitHub notification e-mail) when the login selectors from `app/config_store.py` are gone. It never logs in.
- **Upgrading Playwright:** a new Playwright means a new Chromium. The image runs Python 3.12. Bump the version in `requirements.txt` *and* in the canary workflow, then verify locally (`docker build .`, start the container, and launch Chromium once inside it) before pushing. Dependabot is configured to only propose patch updates for Playwright for this reason, to keep the Python base image on 3.11, and to bundle the remaining updates into one grouped PR per week.
- **Meesman API research (concluded):** the account overview is rendered server-side; there is no JSON API for balances, so DOM scraping stays. The only dynamic JSON calls are Umbraco `Messages/*` endpoints, which are now used for the inbox notifications and as an authenticated session ping. The capture can be re-enabled with `API_CAPTURE=1`.

---

## Security

- Password, TOTP secret and Telegram token are stored **Fernet-encrypted** in `config.yaml`
- The master key is also stored in `config.yaml` — the entire `data/` directory is excluded from Git via `.gitignore`
- Do not expose the container publicly without additional authentication (e.g. a reverse proxy with basic auth or Authelia)

---

## Project structure

```
meesman-tracker/
├── app/
│   ├── main.py              # App bootstrap: lifespan, scheduler jobs, middleware, routers
│   ├── core.py              # Shared base: paths, engine, templates, formatting/parsing helpers
│   ├── store.py             # Storage layer: snapshots, deposits, logs, JSON exports
│   ├── service_refresh.py   # Refresh/keepalive orchestration, failure alerts
│   ├── telegram.py          # Telegram sending and message building
│   ├── routes_dashboard.py  # / , /health, /refresh-now, /session
│   ├── routes_api.py        # /api/*, /export.json|csv, /deposits.json|csv
│   ├── routes_config.py     # /config*
│   ├── routes_deposits.py   # /deposits* (add/update/delete)
│   ├── routes_import.py     # /import*
│   ├── scraper.py           # Playwright scraper, TOTP, HTTP session check
│   ├── db.py                # SQLite schema (WAL, unique index)
│   ├── scheduler.py         # APScheduler instance
│   ├── config_store.py      # YAML config management
│   ├── security.py          # Fernet encryption helpers
│   ├── static/
│   │   ├── app.js           # Dashboard charts (Chart.js) + growth baseline picker
│   │   └── vendor/          # Locally vendored Chart.js (works offline)
│   └── templates/           # Jinja2 HTML templates
├── data/                    # Mounted volume — never committed to Git
├── tests/                   # pytest suite (parsers + smoke tests via TestClient)
│   └── fixtures/            # Offline HTML fixture of the account overview
├── scripts/
│   └── selector_canary.py   # Daily check of the Meesman login selectors
├── .github/
│   ├── workflows/docker.yml           # Tests → multi-arch build (native amd64 + arm64)
│   ├── workflows/selector-canary.yml  # Daily Meesman selector check
│   └── dependabot.yml                 # Weekly dependency update PRs
├── Dockerfile               # tini as PID 1, chromium-headless-shell
├── docker-compose.yml       # Prebuilt image from ghcr.io
├── docker-compose.build.yml # Local development build
├── requirements.txt
├── requirements-dev.txt     # + pytest/httpx/ruff for tests and lint
└── ruff.toml
```

---

## License

MIT
