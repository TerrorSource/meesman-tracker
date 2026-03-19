# Meesman Tracker

A self-hosted Docker application that automatically logs into [mijn.meesman.nl](https://mijn.meesman.nl), scrapes account balances and stores them in a local database. Features a dashboard, REST API for Home Assistant, and Telegram notifications on balance changes.

![Python](https://img.shields.io/badge/python-3.11-blue) ![Docker](https://img.shields.io/badge/docker-compose-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Automatic balance scraping** via Playwright (headless Chromium)
- **TOTP support** — fully unattended, no manual MFA codes needed
- **Session keepalive** — keeps session alive, auto re-login via TOTP on expiry
- **Dashboard** with charts, balance history and return per account
- **Deposit tracking** — distinguish between your own deposits and actual investment returns
- **Home Assistant REST API** — `/api/sensors` and `/deposits.json`
- **Telegram notifications** on balance change (with % vs previous) and session expiry
- **Import** of historical `export.json` and `deposits.json` files
- **Manual data points** — add historical balances for any date

---

## Deployment

### Docker Compose (recommended)

```bash
git clone https://github.com/yourusername/meesman-tracker.git
cd meesman-tracker
docker compose up -d --build
```

Open `http://localhost:8080/config` to complete setup.

### Portainer

1. In Portainer, go to **Stacks** → **Add stack**
2. Name the stack `meesman-tracker`
3. Paste the following into the **Web editor**:

```yaml
services:
  meesman-tracker:
    image: meesman-tracker
    build: .
    container_name: meesman-tracker
    ports:
      - "8080:8080"
    environment:
      - TZ=Europe/Amsterdam
      - DB_PATH=/data/app.db
      - CONFIG_PATH=/data/config.yaml
    volumes:
      - /your/path/to/data:/data
    restart: unless-stopped
```

> **Note:** Replace `/your/path/to/data` with an absolute path on your host, e.g. `/opt/meesman-tracker/data`. Make sure the directory exists before deploying.

4. Click **Deploy the stack**

Alternatively, if you have the repository on your host, use **Repository** mode and point Portainer to your `docker-compose.yml`.

---

## First-time setup

After the container is running, open `http://<host>:8080/config`:

1. **Generate master key** — click *Generate key*. This creates a Fernet encryption key stored in `config.yaml`.
2. **Enter credentials** — your Meesman username and password.
3. **Configure MFA** — choose one method:
   - **TOTP (recommended):** Find the base32 secret in your authenticator app under "manual entry". Enter it in the *TOTP secret* field. The app will generate codes automatically from now on.
   - **Manual code:** Enter a fresh code just before clicking Save, then immediately click *↻ Refresh now*.
4. **Telegram** (optional) — enter your bot token and chat ID to receive balance change notifications.
5. Click **Save** → **↻ Refresh now**

---

## Upgrading to a new version

Keep the contents of your `data/` volume — that is all you need:

| File | Contents |
|---|---|
| `data/config.yaml` | Credentials, TOTP secret, Telegram config (encrypted) |
| `data/export.json` | Full balance history |
| `data/deposits.json` | All deposit records |
| `data/session.json` | Playwright session state (optional, speeds up first login) |
| `data/cookies.json` | Browser cookies (optional) |

**Steps:**
```bash
docker compose down
# Replace application files, keeping your data/ directory intact
docker compose up -d --build
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

---

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/sensors` | GET | All accounts + total (HA-friendly JSON) |
| `/api/sensors/{account_number}` | GET | Single account balance |
| `/api/accounts` | GET | Known account numbers and labels |
| `/export.json` | GET | Full balance history |
| `/deposits.json` | GET | All deposit records |
| `/refresh-now` | POST | Trigger manual refresh |
| `/import` | GET/POST | Import `export.json` files |
| `/import/deposits` | POST | Import `deposits.json` |
| `/import/manual` | POST | Add a single manual data point |
| `/deposits` | GET | Deposit management page |
| `/deposits/add` | POST | Add a deposit |
| `/deposits/delete/{id}` | POST | Delete a deposit |
| `/session` | GET | Session and cookie status |
| `/config` | GET/POST | Configuration page |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TZ` | `UTC` | Timezone (e.g. `Europe/Amsterdam`) |
| `DB_PATH` | `/data/app.db` | SQLite database path |
| `CONFIG_PATH` | `/data/config.yaml` | Configuration file path |
| `DATA_DIR` | `/data` | Base data directory |
| `EXPORT_PATH` | `/data/export.json` | Balance history export |
| `DEPOSITS_PATH` | `/data/deposits.json` | Deposits export |
| `DEBUG_DIR` | `/data/debug` | Screenshots on scrape failure |

---

## Telegram notifications

The app sends a message automatically on:

- **Balance change** — per account with previous/current value, delta and percentage
- **Session expired** — only when using manual MFA; with TOTP the app re-logins automatically

Example message:
```
📊 Meesman balance update — 19-03-2026

📈 Investments (12345678)
   Was: € 20.000,00
   Now: € 20.500,00 (+2.50%)
   Δ:   +€ 500,00

💰 Total: € 45.500,00 (+€ 500,00, +0.26%)
```

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
│   ├── main.py           # FastAPI routes and business logic
│   ├── scraper.py        # Playwright scraper with TOTP support
│   ├── db.py             # SQLite schema
│   ├── scheduler.py      # APScheduler (refresh + keepalive jobs)
│   ├── config_store.py   # YAML config management
│   ├── security.py       # Fernet encryption helpers
│   ├── static/
│   │   └── app.js        # Dashboard charts (Chart.js)
│   └── templates/        # Jinja2 HTML templates
├── data/                 # Mounted volume — never committed to Git
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## License

MIT
