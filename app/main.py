from __future__ import annotations

import json
import logging
import os
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from .db import get_engine, init_db
from .scraper import fetch_accounts, keepalive_session
from .scheduler import scheduler
from .config_store import load_config, save_config
from .security import get_or_create_master_key, encrypt_str, decrypt_str


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("meesman")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR           = Path(os.environ.get("DATA_DIR", "/data"))
EXPORT_PATH        = Path(os.environ.get("EXPORT_PATH",        str(DATA_DIR / "export.json")))
SESSION_STATE_PATH = Path(os.environ.get("SESSION_STATE_PATH", str(DATA_DIR / "session.json")))
COOKIES_DUMP_PATH   = Path(os.environ.get("COOKIES_DUMP_PATH",  str(DATA_DIR / "cookies.json")))
DEPOSITS_PATH       = Path(os.environ.get("DEPOSITS_PATH",      str(DATA_DIR / "deposits.json")))


# ---------------------------------------------------------------------------
# DB + App bootstrap
# ---------------------------------------------------------------------------
engine = get_engine()
init_db(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    hours             = int(cfg.get("refresh_hours")     or 24)
    keepalive_minutes = max(5, int(cfg.get("keepalive_minutes") or 30))

    scheduler.add_job(refresh_once,   "interval", hours=hours,             id="refresh_job",   replace_existing=True)
    scheduler.add_job(keepalive_tick, "interval", minutes=keepalive_minutes, id="keepalive_job", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started (refresh=%dh, keepalive=%dmin)", hours, keepalive_minutes)

    try:
        write_export_json()
    except Exception:
        pass

    # Restore deposits from JSON backup if table is empty
    try:
        n = restore_deposits_from_json()
        if n:
            logger.info("Startup: %d inleggen hersteld uit deposits.json", n)
    except Exception:
        pass

    yield

    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_timedelta(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def cfg_has_key(cfg: dict) -> bool:
    return bool((cfg.get("master_key") or "").strip())


def decrypt_if_present(enc: str | None) -> str:
    if not enc:
        return ""
    try:
        return decrypt_str(enc)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Cookie / session summary
# ---------------------------------------------------------------------------
def read_cookie_dump_summary() -> dict:
    out = {
        "path":    str(COOKIES_DUMP_PATH),
        "exists":  COOKIES_DUMP_PATH.exists(),
        "mtime":   None,
        "count":   0,
        "cookies": [],
        "soonest_expires_at": None,
        "latest_expires_at":  None,
    }

    if not COOKIES_DUMP_PATH.exists():
        return out

    try:
        out["mtime"] = datetime.fromtimestamp(
            COOKIES_DUMP_PATH.stat().st_mtime, tz=timezone.utc
        ).isoformat()

        raw = json.loads(COOKIES_DUMP_PATH.read_text(encoding="utf-8"))
        cookies = raw.get("cookies", []) if isinstance(raw, dict) else []
        out["count"] = len(cookies)
        now_ts = datetime.now(timezone.utc).timestamp()

        soonest = latest = None

        for c in cookies:
            exp = c.get("expires")
            exp_iso = remaining = None
            if isinstance(exp, (int, float)) and exp and exp > 0:
                exp_dt  = datetime.fromtimestamp(float(exp), tz=timezone.utc)
                exp_iso = exp_dt.isoformat()
                remaining = _fmt_timedelta(float(exp) - now_ts)
                soonest = exp_dt if soonest is None else min(soonest, exp_dt)
                latest  = exp_dt if latest  is None else max(latest,  exp_dt)

            out["cookies"].append({
                "name":       c.get("name"),
                "domain":     c.get("domain"),
                "path":       c.get("path"),
                "expires_at": exp_iso,
                "expires_in": remaining,
            })

        out["soonest_expires_at"] = soonest.isoformat() if soonest else None
        out["latest_expires_at"]  = latest.isoformat()  if latest  else None

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    return out


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def write_refresh_log(status: str, stored_rows: int, message: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO refresh_log (ts, status, stored_rows, message) VALUES (:ts, :st, :n, :msg)"),
            {"ts": now_iso(), "st": status, "n": int(stored_rows), "msg": message},
        )


def write_keepalive_log(status: str, message: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO keepalive_log (ts, status, message) VALUES (:ts, :st, :msg)"),
            {"ts": now_iso(), "st": status, "msg": message},
        )


def get_prev_values() -> dict[str, float]:
    """Return {account_number: last_value_eur} for all accounts."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, value_eur
            FROM accounts_snapshot
            WHERE id IN (
                SELECT MAX(id) FROM accounts_snapshot GROUP BY account_number
            )
        """)).mappings().all()
    return {r["account_number"]: float(str(r["value_eur"]).replace(",", ".")) for r in rows}


# ---------------------------------------------------------------------------
# Export JSON
# ---------------------------------------------------------------------------
def build_export_payload() -> dict:
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT ts, account_number, label, value_eur
            FROM accounts_snapshot
            ORDER BY account_number, ts
        """)).mappings().all()

    series: dict[str, list] = {}
    labels: dict[str, str]  = {}
    latest: dict[str, dict] = {}

    for r in rows:
        acc = r["account_number"]
        labels[acc] = r["label"]
        pt = {"ts": r["ts"], "value_eur": float(str(r["value_eur"]).replace(",", "."))}
        series.setdefault(acc, []).append(pt)
        latest[acc] = pt

    return {
        "generated_at": now_iso(),
        "accounts": [
            {
                "account_number": acc,
                "label":          labels.get(acc, ""),
                "latest":         latest.get(acc),
                "history":        series[acc],
            }
            for acc in sorted(series)
        ],
    }


def write_export_json() -> None:
    payload = build_export_payload()
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Deposits JSON (portable backup — used for HA and version migration)
# ---------------------------------------------------------------------------
def write_deposits_json() -> None:
    """Write all deposits to /data/deposits.json."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT ts, account_number, label, amount_eur, note
            FROM deposits ORDER BY account_number, ts
        """)).mappings().all()

    entries = [
        {
            "ts":             r["ts"],
            "account_number": r["account_number"],
            "label":          r["label"],
            "amount_eur":     float(str(r["amount_eur"]).replace(",", ".")),
            "note":           r["note"] or "",
        }
        for r in rows
    ]
    payload = {"generated_at": now_iso(), "deposits": entries}
    DEPOSITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEPOSITS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("deposits.json bijgewerkt: %d inleggen", len(entries))


def restore_deposits_from_json() -> int:
    """
    Import deposits.json into the DB on startup if the deposits table is empty.
    Returns number of rows inserted.
    """
    if not DEPOSITS_PATH.exists():
        return 0
    try:
        payload = json.loads(DEPOSITS_PATH.read_text(encoding="utf-8"))
        entries = payload.get("deposits", [])
        if not entries:
            return 0

        inserted = 0
        with engine.begin() as conn:
            # Only restore if table is empty
            count = conn.execute(text("SELECT COUNT(*) FROM deposits")).scalar()
            if count and count > 0:
                logger.info("deposits tabel heeft al %d rijen — restore overgeslagen", count)
                return 0

            for e in entries:
                ts  = (e.get("ts") or "").strip()
                acc = (e.get("account_number") or "").strip()
                lbl = (e.get("label") or acc).strip()
                amt = float(str(e.get("amount_eur", 0)).replace(",", "."))
                note = (e.get("note") or "").strip() or None
                if not ts or not acc or amt <= 0:
                    continue
                conn.execute(text("""
                    INSERT INTO deposits (ts, account_number, label, amount_eur, note)
                    VALUES (:ts, :n, :l, :v, :note)
                """), {"ts": ts, "n": acc, "l": lbl, "v": amt, "note": note})
                inserted += 1

        logger.info("deposits.json hersteld: %d inleggen geïmporteerd", inserted)
        return inserted
    except Exception as e:
        logger.warning("deposits.json restore mislukt: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def telegram_enabled(cfg: dict) -> bool:
    return bool(
        (cfg.get("telegram_bot_token_enc") or "").strip()
        and (cfg.get("telegram_chat_id_enc") or "").strip()
    )


def send_telegram(cfg: dict, message: str) -> tuple[bool, str]:
    """Send a plain-text Telegram message. Never raises."""
    try:
        if not telegram_enabled(cfg):
            return False, "Telegram not configured"

        token   = decrypt_if_present(cfg.get("telegram_bot_token_enc")).strip()
        chat_id = decrypt_if_present(cfg.get("telegram_chat_id_enc")).strip()
        if not token or not chat_id:
            return False, "Token/chat_id missing"

        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
        if 200 <= r.status_code < 300:
            return True, "Sent"

        body = (r.text or "")[:300]
        return False, f"HTTP {r.status_code}: {body}"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _fmt_eur(v: float) -> str:
    """Format as Dutch currency: 30180.36 → '€ 30.180,36'"""
    formatted = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {formatted}"


def build_balance_change_message(
    accounts: list,        # list of AccountRow
    prev_values: dict,     # {account_number: float}
    all_prev_values: dict, # {account_number: float} — for total prev calculation
) -> str | None:
    """
    Build a Telegram message if any account balance changed.
    Returns None if nothing changed.
    """
    lines      = []
    total      = 0.0
    total_prev = 0.0
    any_change = False

    date_str = datetime.now(timezone.utc).strftime("%d-%m-%Y")

    for a in sorted(accounts, key=lambda x: x.account_number):
        total += a.value_eur
        prev = prev_values.get(a.account_number)
        total_prev += prev if prev is not None else a.value_eur

        if prev is None:
            lines.append(f"🆕 {a.label} ({a.account_number})\n   Nu: {_fmt_eur(a.value_eur)}")
            any_change = True
        elif abs(a.value_eur - prev) < 0.005:
            lines.append(f"➡️  {a.label}: {_fmt_eur(a.value_eur)} (ongewijzigd)")
        else:
            delta = a.value_eur - prev
            pct   = (delta / prev * 100) if prev else 0.0
            sign  = "+" if delta >= 0 else ""
            arrow = "📈" if delta >= 0 else "📉"
            lines.append(
                f"{arrow} {a.label} ({a.account_number})\n"
                f"   Was: {_fmt_eur(prev)}\n"
                f"   Nu:  {_fmt_eur(a.value_eur)} ({sign}{pct:.2f}%)\n"
                f"   Δ:   {sign}{_fmt_eur(delta)}"
            )
            any_change = True

    if not any_change:
        return None

    total_delta = total - total_prev
    total_pct   = (total_delta / total_prev * 100) if total_prev else 0.0
    sign        = "+" if total_delta >= 0 else ""

    header = f"📊 Meesman saldo update — {date_str}"
    footer = f"\n💰 Totaal: {_fmt_eur(total)}"
    if abs(total_delta) >= 0.01:
        footer += f" ({sign}{_fmt_eur(total_delta)}, {sign}{total_pct:.2f}%)"

    return header + "\n\n" + "\n\n".join(lines) + "\n" + footer


# ---------------------------------------------------------------------------
# Core refresh logic
# ---------------------------------------------------------------------------
async def refresh_once() -> None:
    cfg = load_config()

    if not cfg_has_key(cfg):
        logger.info("Refresh: no master key, skipping.")
        write_refresh_log("skipped", 0, "No master key configured yet")
        return

    username = (cfg.get("username") or "").strip()
    password = decrypt_if_present(cfg.get("password_enc"))

    if not username or not password:
        logger.info("Refresh: username/password missing, skipping.")
        write_refresh_log("skipped", 0, "Missing username/password")
        return

    mfa_mode = cfg.get("mfa_mode", "manual")

    # Validate MFA config before hitting the browser
    if mfa_mode == "manual":
        mfa_code = decrypt_if_present(cfg.get("manual_mfa_code_enc")).strip()
        if not mfa_code:
            msg = "Handmatige MFA-code vereist. Voer een nieuwe in via /config."
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            return
        totp_secret = ""
    elif mfa_mode == "totp":
        totp_secret = decrypt_if_present(cfg.get("totp_secret_enc")).strip()
        mfa_code    = ""
        if not totp_secret:
            msg = "TOTP-geheim niet ingesteld. Stel het in via /config."
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            return
    else:
        mfa_code = totp_secret = ""

    logger.info("Refresh: starting (mfa_mode=%s)", mfa_mode)

    try:
        sels = cfg.get("selectors") or {}
        scrape_cfg = {
            "username":    username,
            "password":    password,
            "mfa_mode":    mfa_mode,
            "mfa_code":    mfa_code,
            "totp_secret": totp_secret,
            **{k: sels[k] for k in sels},
        }

        accounts = await fetch_accounts(
            scrape_cfg,
            storage_state_path=str(SESSION_STATE_PATH),
            dump_cookies_path=str(COOKIES_DUMP_PATH),
        )

        if not accounts:
            msg = "Scrape leverde 0 rekeningen op (login/MFA/selectors mislukt)"
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            return

        # ------------------------------------------------------------------
        # Compare with previous values (only store when changed)
        # ------------------------------------------------------------------
        prev_values = get_prev_values()
        ts = now_iso()
        stored = 0

        with engine.begin() as conn:
            for a in accounts:
                prev = prev_values.get(a.account_number)
                if prev is None or abs(a.value_eur - prev) >= 0.005:
                    conn.execute(
                        text("INSERT INTO accounts_snapshot (ts, account_number, label, value_eur) "
                             "VALUES (:ts, :n, :l, :v)"),
                        {"ts": ts, "n": a.account_number, "l": a.label, "v": a.value_eur},
                    )
                    stored += 1

        write_export_json()
        logger.info("Refresh: %d rekeningen opgehaald, %d opgeslagen op %s", len(accounts), stored, ts)
        write_refresh_log("ok", stored, None)

        # ------------------------------------------------------------------
        # Telegram notification on balance change
        # ------------------------------------------------------------------
        if telegram_enabled(cfg):
            msg = build_balance_change_message(accounts, prev_values, prev_values)
            if msg:
                ok, info = send_telegram(cfg, msg)
                logger.info("Telegram balance update: ok=%s info=%s", ok, info)

    except Exception as e:
        msg = f"Onverwachte fout: {type(e).__name__}: {e}"
        logger.exception("Refresh: %s", msg)
        write_refresh_log("failed", 0, msg)


async def keepalive_tick() -> None:
    """
    Periodically reuse stored session to keep it warm (no DB writes).
    If the session is expired and TOTP is configured, automatically re-logins.
    """
    cfg = load_config()
    if not cfg_has_key(cfg):
        return

    sels = cfg.get("selectors") or {}
    try:
        ok = await keepalive_session(
            {"accounts_row_selector": sels.get("accounts_row_selector", "")},
            storage_state_path=str(SESSION_STATE_PATH),
            dump_cookies_path=str(COOKIES_DUMP_PATH),
        )
    except Exception:
        ok = False

    if ok:
        logger.info("Keepalive: OK")
        write_keepalive_log("ok")
        return

    # Session expired — try to recover automatically if TOTP is configured
    logger.warning("Keepalive: sessie verlopen.")
    write_keepalive_log("failed", "Sessie verlopen (keepalive)")

    mfa_mode    = cfg.get("mfa_mode", "manual")
    totp_secret = decrypt_if_present(cfg.get("totp_secret_enc")).strip()

    if mfa_mode == "totp" and totp_secret:
        logger.info("Keepalive: TOTP beschikbaar — automatisch opnieuw inloggen.")
        write_refresh_log("session_expired", 0, "Sessie verlopen — automatisch herstel gestart (TOTP)")
        await refresh_once()
        write_keepalive_log("recovered", "Sessie automatisch hersteld via TOTP")
    else:
        # Manual MFA: we can't re-login automatically, notify the user
        write_refresh_log("session_expired", 0, "Sessie verlopen (keepalive) — handmatige actie vereist")
        send_telegram(
            cfg,
            "⚠️ Meesman-tracker: sessie verlopen.\n\n"
            "Open /config, voer een nieuwe MFA-code in, sla op en klik op 'Refresh now'.",
        )




def get_deposits() -> dict[str, float]:
    """Return {account_number: total_deposited_eur}"""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, SUM(amount_eur) as total
            FROM deposits GROUP BY account_number
        """)).mappings().all()
    return {r["account_number"]: float(r["total"]) for r in rows}

# ---------------------------------------------------------------------------
# Routes — dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with engine.begin() as conn:
        last = conn.execute(text("""
            SELECT ts, status, stored_rows, message
            FROM refresh_log ORDER BY id DESC LIMIT 1
        """)).mappings().first()

        rows = conn.execute(text("""
            SELECT ts, account_number, label, value_eur
            FROM accounts_snapshot ORDER BY account_number, ts
        """)).mappings().all()

    series    = {}
    last_val  = {}
    first_val = {}
    changes   = {}
    labels    = {}

    for r in rows:
        acc = r["account_number"]
        labels[acc] = r["label"]
        val = float(str(r["value_eur"]).replace(",", "."))  # SQLite kan tekst of komma-notatie teruggeven

        if acc not in first_val:
            first_val[acc] = {"ts": r["ts"], "value": val}

        prev = last_val.get(acc)
        if prev is None or prev != val:
            # Only add to chart series when value actually changes
            series.setdefault(acc, []).append({"x": r["ts"], "y": val})

            prev_value = changes[acc][-1]["value"] if changes.get(acc) else None
            delta      = (val - prev_value) if prev_value is not None else None
            delta_pct  = (delta / prev_value * 100) if prev_value else None
            changes.setdefault(acc, []).append({
                "ts":        r["ts"],
                "value":     val,
                "delta":     delta,
                "delta_pct": delta_pct,
            })
        last_val[acc] = val

    accounts_payload = []
    for acc in sorted(series):
        current   = last_val.get(acc)
        first     = first_val.get(acc, {})
        first_v   = first.get("value")
        total_delta     = (current - first_v)           if current is not None and first_v else None
        total_delta_pct = (total_delta / first_v * 100) if first_v else None
        accounts_payload.append({
            "account_number":  acc,
            "label":           labels.get(acc, ""),
            "points":          series[acc],
            "changes":         changes.get(acc, []),
            "current":         current,
            "first_ts":        first.get("ts"),
            "first_value":     first_v,
            "total_delta":     total_delta,
            "total_delta_pct": total_delta_pct,
        })

    deposits = get_deposits()
    for a in accounts_payload:
        total_dep = deposits.get(a["account_number"])
        a["total_deposits"]  = total_dep
        a["true_rendement"]  = (a["current"] - total_dep) if (a["current"] is not None and total_dep is not None) else None
        a["true_rendement_pct"] = ((a["true_rendement"] / total_dep) * 100) if (a["true_rendement"] is not None and total_dep) else None

    payload = {"accounts": accounts_payload}

    return templates.TemplateResponse("dashboard.html", {
        "request":      request,
        "payload_json": json.dumps(payload),
        "last_refresh": dict(last) if last else None,
        "export_path":  "/export.json",
    })


@app.post("/refresh-now")
async def refresh_now():
    logger.info("Handmatige refresh gestart.")
    await refresh_once()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Routes — export / HA sensor API
# ---------------------------------------------------------------------------
@app.get("/export.json")
def export_json():
    if not EXPORT_PATH.exists():
        try:
            write_export_json()
        except Exception:
            raise HTTPException(status_code=404, detail="export.json nog niet beschikbaar")
    return FileResponse(str(EXPORT_PATH), media_type="application/json", filename="export.json")


@app.get("/api/sensors")
def api_sensors():
    """
    Home Assistant REST sensor endpoint.

    HA configuration example (configuration.yaml):

      sensor:
        - platform: rest
          resource: http://<host>:8080/api/sensors
          name: Meesman
          json_attributes:
            - accounts
            - total
          value_template: "{{ value_json.total }}"
          unit_of_measurement: "EUR"
          scan_interval: 3600

    Per-rekening via template sensor:
      - platform: template
        sensors:
          meesman_beleggingen:
            value_template: >
              {{ state_attr('sensor.meesman', 'accounts')
                 | selectattr('account_number','eq','22404586')
                 | map(attribute='value_eur') | first }}
            unit_of_measurement: "EUR"
    """
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, label, value_eur, ts
            FROM accounts_snapshot
            WHERE id IN (
                SELECT MAX(id) FROM accounts_snapshot GROUP BY account_number
            )
            ORDER BY account_number
        """)).mappings().all()

    accounts = [
        {
            "account_number": r["account_number"],
            "label":          r["label"],
            "value_eur":      float(str(r["value_eur"]).replace(",", ".")),
            "last_updated":   r["ts"],
        }
        for r in rows
    ]
    total = sum(a["value_eur"] for a in accounts)

    return JSONResponse({"total": round(total, 2), "accounts": accounts})


@app.get("/api/sensors/{account_number}")
def api_sensor_account(account_number: str):
    """
    Single-account HA REST sensor.

    HA configuration example:

      sensor:
        - platform: rest
          resource: http://<host>:8080/api/sensors/22404586
          name: Meesman Beleggingen
          value_template: "{{ value_json.value_eur }}"
          unit_of_measurement: "EUR"
          scan_interval: 3600
    """
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT account_number, label, value_eur, ts
            FROM accounts_snapshot
            WHERE account_number = :n
            ORDER BY id DESC LIMIT 1
        """), {"n": account_number}).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Rekening {account_number} niet gevonden")

    return JSONResponse({
        "account_number": row["account_number"],
        "label":          row["label"],
        "value_eur":      row["value_eur"],
        "last_updated":   row["ts"],
    })


# ---------------------------------------------------------------------------
# Routes — session
# ---------------------------------------------------------------------------
@app.get("/session", response_class=HTMLResponse)
def session_page(request: Request):
    try:
        cfg = load_config()
        keepalive_minutes = max(5, int(cfg.get("keepalive_minutes") or 30))

        with engine.begin() as conn:
            keepalive_rows = conn.execute(text("""
                SELECT ts, status, message FROM keepalive_log ORDER BY id DESC LIMIT 20
            """)).mappings().all()

        cookie_summary = read_cookie_dump_summary()

        session_exists = SESSION_STATE_PATH.exists()
        session_mtime  = None
        if session_exists:
            try:
                session_mtime = datetime.fromtimestamp(
                    SESSION_STATE_PATH.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        return templates.TemplateResponse("session.html", {
            "request":           request,
            "now_utc":           now_iso(),
            "keepalive_minutes": keepalive_minutes,
            "session_path":      str(SESSION_STATE_PATH),
            "session_exists":    session_exists,
            "session_mtime":     session_mtime,
            "cookie_summary":    cookie_summary,
            "keepalive_rows":    [dict(r) for r in keepalive_rows],
        })
    except Exception as e:
        logger.exception("Session page fout: %s", e)
        return HTMLResponse(
            content=f"<h2>Fout</h2><pre>{type(e).__name__}: {e}</pre>",
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Routes — config
# ---------------------------------------------------------------------------
@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    cfg = load_config()

    view = {
        "has_key":          cfg_has_key(cfg),
        "username":         cfg.get("username") or "",
        "refresh_hours":    cfg.get("refresh_hours") or 24,
        "keepalive_minutes": cfg.get("keepalive_minutes") or 30,
        "password_set":     bool((cfg.get("password_enc") or "").strip()),
        "mfa_mode":         cfg.get("mfa_mode") or "manual",

        # TOTP
        "totp_secret_set":  bool((cfg.get("totp_secret_enc") or "").strip()),
        "totp_secret":      decrypt_if_present(cfg.get("totp_secret_enc")).strip(),

        # Manual MFA
        "manual_mfa_set":   bool((cfg.get("manual_mfa_code_enc") or "").strip()),
        "manual_mfa_code":  decrypt_if_present(cfg.get("manual_mfa_code_enc")).strip(),

        # Telegram
        "telegram_bot_set":  bool((cfg.get("telegram_bot_token_enc") or "").strip()),
        "telegram_chat_set": bool((cfg.get("telegram_chat_id_enc") or "").strip()),
        "telegram_bot_token": decrypt_if_present(cfg.get("telegram_bot_token_enc")).strip(),
        "telegram_chat_id":   decrypt_if_present(cfg.get("telegram_chat_id_enc")).strip(),
    }
    return templates.TemplateResponse("config.html", {"request": request, "cfg": view})


@app.post("/config/generate-key")
def generate_key():
    get_or_create_master_key(create=True)
    logger.info("Master key aangemaakt.")
    return RedirectResponse(url="/config?saved=1", status_code=303)


@app.post("/config/save")
def config_save(
    username:          str = Form(""),
    password:          str = Form(""),
    refresh_hours:     int = Form(24),
    keepalive_minutes: int = Form(30),
    mfa_mode:          str = Form("manual"),
    totp_secret:       str = Form(""),
    manual_mfa_code:   str = Form(""),
    telegram_bot_token: str = Form(""),
    telegram_chat_id:   str = Form(""),
):
    cfg = load_config()

    if not cfg_has_key(cfg):
        return RedirectResponse(url="/config?error=no_key", status_code=303)

    cfg["username"]          = username.strip()
    cfg["refresh_hours"]     = int(refresh_hours)
    cfg["keepalive_minutes"] = max(5, int(keepalive_minutes))
    cfg["mfa_mode"]          = mfa_mode.strip() or "manual"

    pw = (password or "").strip()
    if pw and pw != "********":
        cfg["password_enc"] = encrypt_str(pw)

    if totp_secret.strip():
        cfg["totp_secret_enc"] = encrypt_str(totp_secret.strip())

    if manual_mfa_code.strip():
        cfg["manual_mfa_code_enc"] = encrypt_str(manual_mfa_code.strip())

    if telegram_bot_token.strip():
        cfg["telegram_bot_token_enc"] = encrypt_str(telegram_bot_token.strip())
    if telegram_chat_id.strip():
        cfg["telegram_chat_id_enc"] = encrypt_str(telegram_chat_id.strip())

    save_config(cfg)

    # Clear keepalive history after config change
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM keepalive_log"))
    except Exception:
        pass

    scheduler.reschedule_job("refresh_job",   trigger="interval", hours=int(refresh_hours))
    scheduler.reschedule_job("keepalive_job", trigger="interval", minutes=max(5, int(keepalive_minutes)))
    logger.info("Config opgeslagen. refresh=%dh keepalive=%dmin mfa_mode=%s", int(refresh_hours), max(5, int(keepalive_minutes)), mfa_mode)

    return RedirectResponse(url="/config?saved=1", status_code=303)


@app.post("/config/test-telegram")
async def config_test_telegram():
    cfg = load_config()
    ok, _ = send_telegram(cfg, "✅ meesman-tracker Telegram test")
    return RedirectResponse(url=f"/config?tg_test={'1' if ok else '0'}&tg_err={'0' if ok else '1'}", status_code=303)

# ---------------------------------------------------------------------------
# Routes — import
# ---------------------------------------------------------------------------
@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return templates.TemplateResponse("import.html", {"request": request})


@app.post("/import")
async def import_post(request: Request, files: list[UploadFile] = File(...)):
    """
    Accept one or more export.json files.
    Deduplicates on (ts, account_number) — skips rows that already exist.
    Returns a summary per file.
    """
    results = []

    for upload in files:
        fname = upload.filename or "onbekend"
        try:
            raw     = await upload.read()
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            results.append({"file": fname, "error": f"Ongeldig JSON: {e}", "inserted": 0, "skipped": 0})
            continue

        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            results.append({"file": fname, "error": "Geen 'accounts' lijst gevonden", "inserted": 0, "skipped": 0})
            continue

        inserted = skipped = 0

        with engine.begin() as conn:
            for acc in accounts:
                acc_number = acc.get("account_number", "").strip()
                label      = acc.get("label", "").strip()
                history    = acc.get("history") or []

                # Also include the 'latest' point if not already in history
                latest = acc.get("latest")
                if latest and isinstance(latest, dict):
                    history = list(history)
                    if not any(h.get("ts") == latest.get("ts") for h in history):
                        history.append(latest)

                for pt in history:
                    ts        = (pt.get("ts") or "").strip()
                    value_eur = pt.get("value_eur")

                    if not ts or value_eur is None or not acc_number:
                        skipped += 1
                        continue

                    # Deduplicate: skip if (ts, account_number) already exists
                    exists = conn.execute(text("""
                        SELECT 1 FROM accounts_snapshot
                        WHERE ts = :ts AND account_number = :n
                        LIMIT 1
                    """), {"ts": ts, "n": acc_number}).first()

                    if exists:
                        skipped += 1
                        continue

                    conn.execute(text("""
                        INSERT INTO accounts_snapshot (ts, account_number, label, value_eur)
                        VALUES (:ts, :n, :l, :v)
                    """), {"ts": ts, "n": acc_number, "l": label, "v": float(str(value_eur).replace(",", "."))})
                    inserted += 1

        results.append({"file": fname, "error": None, "inserted": inserted, "skipped": skipped})
        logger.info("Import %s: %d ingevoerd, %d overgeslagen", fname, inserted, skipped)

    # Rebuild export.json after import
    if any(r["inserted"] > 0 for r in results):
        try:
            write_export_json()
        except Exception:
            pass

    total_inserted = sum(r["inserted"] for r in results)
    total_skipped  = sum(r["skipped"]  for r in results)

    return templates.TemplateResponse("import.html", {
        "request":        request,
        "results":        results,
        "total_inserted": total_inserted,
        "total_skipped":  total_skipped,
    })

@app.get("/api/accounts")
def api_accounts():
    """Returns known account numbers + labels for the manual entry form."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT account_number, label
            FROM accounts_snapshot
            ORDER BY account_number
        """)).mappings().all()
    return JSONResponse([{"account_number": r["account_number"], "label": r["label"]} for r in rows])


@app.post("/import/manual")
async def import_manual(
    request:        Request,
    account_number: str   = Form(...),
    label:          str   = Form(""),
    entry_date:     str   = Form(...),   # YYYY-MM-DD
    entry_time:     str   = Form("00:00"),
    value_eur:      str   = Form(...),
):
    """Manually add a single historical data point."""
    acc_number = account_number.strip()
    if not acc_number:
        return templates.TemplateResponse("import.html", {
            "request": request,
            "manual_error": "Rekeningnummer is verplicht.",
            "manual_inserted": 0,
        })

    # Build ISO timestamp in UTC
    try:
        dt  = datetime.fromisoformat(f"{entry_date}T{entry_time}:00").replace(tzinfo=timezone.utc)
        ts  = dt.isoformat()
    except ValueError:
        return templates.TemplateResponse("import.html", {
            "request": request,
            "manual_error": "Ongeldige datum of tijd.",
            "manual_inserted": 0,
        })

    # Parse Dutch number format only: "29.869,81" or "29869,81"
    try:
        import re as _re
        v = value_eur.strip().replace("€", "").replace("\u00a0", " ").strip()
        v = _re.sub(r"[^\d,\-]", "", v)   # strip everything except digits, comma, minus
        if v.count(",") > 1 or "," not in v:
            raise ValueError("Gebruik komma als decimaalteken, bijv. 4987,50")
        v = v.replace(",", ".")
        value_eur_float = float(v)
    except (ValueError, AttributeError) as _e:
        return templates.TemplateResponse("import.html", {
            "request": request,
            "manual_error": f"Ongeldig bedrag: '{value_eur}'. Gebruik komma als decimaalteken, bijv. 4987,50 of 29869,81",
            "manual_inserted": 0,
        })

    with engine.begin() as conn:
        # Look up existing label if not provided
        if not label.strip():
            row = conn.execute(text("""
                SELECT label FROM accounts_snapshot
                WHERE account_number = :n ORDER BY id DESC LIMIT 1
            """), {"n": acc_number}).first()
            label = row[0] if row else acc_number

        # Deduplicate
        exists = conn.execute(text("""
            SELECT 1 FROM accounts_snapshot
            WHERE ts = :ts AND account_number = :n LIMIT 1
        """), {"ts": ts, "n": acc_number}).first()

        if exists:
            return templates.TemplateResponse("import.html", {
                "request": request,
                "manual_error": f"Er bestaat al een datapunt voor {acc_number} op {ts}.",
                "manual_inserted": 0,
            })

        conn.execute(text("""
            INSERT INTO accounts_snapshot (ts, account_number, label, value_eur)
            VALUES (:ts, :n, :l, :v)
        """), {"ts": ts, "n": acc_number, "l": label.strip() or acc_number, "v": value_eur})

    try:
        write_export_json()
    except Exception:
        pass

    logger.info("Handmatig datapunt toegevoegd: %s %s € %.2f", acc_number, ts, value_eur_float)

    return templates.TemplateResponse("import.html", {
        "request":         request,
        "manual_inserted": 1,
        "manual_ts":       ts,
        "manual_account":  acc_number,
        "manual_value":    value_eur_float,
    })

# ---------------------------------------------------------------------------
# Routes — deposits (inleg)
# ---------------------------------------------------------------------------
@app.get("/deposits", response_class=HTMLResponse)
def deposits_page(request: Request):
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, ts, account_number, label, amount_eur, note
            FROM deposits ORDER BY ts DESC
        """)).mappings().all()
    return templates.TemplateResponse("deposits.html", {
        "request": request,
        "deposits": [dict(r) for r in rows],
    })


@app.post("/deposits/add")
async def deposits_add(
    request:        Request,
    account_number: str   = Form(...),
    entry_date:     str   = Form(...),
    entry_time:     str   = Form("00:00"),
    amount_eur:     str   = Form(...),
    note:           str   = Form(""),
):
    acc_number = account_number.strip()
    if not acc_number:
        return templates.TemplateResponse("deposits.html", {
            "request": request, "error": "Rekeningnummer is verplicht.", "deposits": [],
        })

    try:
        dt = datetime.fromisoformat(f"{entry_date}T{entry_time}:00").replace(tzinfo=timezone.utc)
        ts = dt.isoformat()
    except ValueError:
        return templates.TemplateResponse("deposits.html", {
            "request": request, "error": "Ongeldige datum of tijd.", "deposits": [],
        })

    try:
        import re as _re
        v = amount_eur.strip().replace("€", "").replace("\u00a0", " ").strip()
        v = _re.sub(r"[^\d,\-]", "", v)
        if "," not in v:
            raise ValueError
        amount_float = float(v.replace(",", "."))
    except (ValueError, AttributeError):
        return templates.TemplateResponse("deposits.html", {
            "request": request, "error": f"Ongeldig bedrag '{amount_eur}'. Gebruik komma: bijv. 4987,50", "deposits": [],
        })

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT label FROM accounts_snapshot
            WHERE account_number = :n ORDER BY id DESC LIMIT 1
        """), {"n": acc_number}).first()
        label = row[0] if row else acc_number

        conn.execute(text("""
            INSERT INTO deposits (ts, account_number, label, amount_eur, note)
            VALUES (:ts, :n, :l, :v, :note)
        """), {"ts": ts, "n": acc_number, "l": label, "v": amount_float, "note": note.strip() or None})

    logger.info("Inleg toegevoegd: %s %s € %.2f", acc_number, ts, amount_float)
    try:
        write_deposits_json()
    except Exception:
        pass
    return RedirectResponse(url="/deposits?saved=1", status_code=303)


@app.post("/deposits/delete/{deposit_id}")
async def deposits_delete(deposit_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM deposits WHERE id = :id"), {"id": deposit_id})
    logger.info("Inleg verwijderd: id=%d", deposit_id)
    try:
        write_deposits_json()
    except Exception:
        pass
    return RedirectResponse(url="/deposits?deleted=1", status_code=303)

@app.get("/deposits.json")
def deposits_json_endpoint():
    """
    Serves /data/deposits.json for Home Assistant or backup purposes.

    HA configuration example:
      sensor:
        - platform: rest
          resource: http://<host>:8080/deposits.json
          name: Meesman Inleg
          json_attributes:
            - deposits
          value_template: >
            {{ value_json.deposits
               | selectattr('account_number','eq','22404586')
               | map(attribute='amount_eur') | sum | round(2) }}
          unit_of_measurement: "EUR"
    """
    if not DEPOSITS_PATH.exists():
        try:
            write_deposits_json()
        except Exception:
            raise HTTPException(status_code=404, detail="deposits.json nog niet beschikbaar")
    return FileResponse(str(DEPOSITS_PATH), media_type="application/json", filename="deposits.json")


@app.post("/import/deposits")
async def import_deposits(request: Request, files: list[UploadFile] = File(...)):
    """Import one or more deposits.json files. Deduplicates on (ts, account_number, amount_eur)."""
    results = []

    for upload in files:
        fname = upload.filename or "onbekend"
        try:
            raw     = await upload.read()
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            results.append({"file": fname, "error": f"Ongeldig JSON: {e}", "inserted": 0, "skipped": 0})
            continue

        entries = payload.get("deposits")
        if not isinstance(entries, list):
            results.append({"file": fname, "error": "Geen 'deposits' lijst gevonden", "inserted": 0, "skipped": 0})
            continue

        inserted = skipped = 0
        with engine.begin() as conn:
            for e in entries:
                ts  = (e.get("ts") or "").strip()
                acc = (e.get("account_number") or "").strip()
                lbl = (e.get("label") or acc).strip()
                amt = float(str(e.get("amount_eur", 0)).replace(",", "."))
                note = (e.get("note") or "").strip() or None

                if not ts or not acc or amt <= 0:
                    skipped += 1
                    continue

                exists = conn.execute(text("""
                    SELECT 1 FROM deposits
                    WHERE ts = :ts AND account_number = :n AND amount_eur = :v
                    LIMIT 1
                """), {"ts": ts, "n": acc, "v": amt}).first()

                if exists:
                    skipped += 1
                    continue

                conn.execute(text("""
                    INSERT INTO deposits (ts, account_number, label, amount_eur, note)
                    VALUES (:ts, :n, :l, :v, :note)
                """), {"ts": ts, "n": acc, "l": lbl, "v": amt, "note": note})
                inserted += 1

        results.append({"file": fname, "error": None, "inserted": inserted, "skipped": skipped})
        logger.info("Deposits import %s: %d ingevoerd, %d overgeslagen", fname, inserted, skipped)

    if any(r["inserted"] > 0 for r in results):
        try:
            write_deposits_json()
        except Exception:
            pass

    total_inserted = sum(r["inserted"] for r in results)
    total_skipped  = sum(r["skipped"]  for r in results)

    return templates.TemplateResponse("import.html", {
        "request":                  request,
        "dep_results":              results,
        "dep_total_inserted":       total_inserted,
        "dep_total_skipped":        total_skipped,
    })
