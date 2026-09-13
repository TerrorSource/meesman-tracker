"""Opslaglaag: logtabellen, snapshots, deposits en de JSON-exports."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from datetime import timedelta
from pathlib import Path

from .core import (
    COOKIES_DUMP_PATH,
    DEBUG_DIR,
    DEPOSITS_PATH,
    EXPORT_PATH,
    engine,
    fmt_timedelta,
    logger,
    now_iso,
    to_float,
)

# Foutmeldingen inkorten: een Chromium-commandoregel van 3 KB heeft geen
# nut in de database of op het dashboard.
_MAX_LOG_MESSAGE = 500


# ---------------------------------------------------------------------------
# Logtabellen
# ---------------------------------------------------------------------------
def write_refresh_log(status: str, stored_rows: int, message: str | None = None) -> None:
    if message and len(message) > _MAX_LOG_MESSAGE:
        message = message[:_MAX_LOG_MESSAGE] + " …"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO refresh_log (ts, status, stored_rows, message) VALUES (:ts, :st, :n, :msg)"),
            {"ts": now_iso(), "st": status, "n": int(stored_rows), "msg": message},
        )


def last_refresh_info() -> dict | None:
    """Laatste refresh-log-regel (ts, status, message) of None."""
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT ts, status, stored_rows, message FROM refresh_log ORDER BY id DESC LIMIT 1"
        )).mappings().first()
    return dict(row) if row else None


def prune_old_logs(days: int = 90) -> int:
    """Verwijder log-regels ouder dan `days` (tabellen groeien anders onbegrensd)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    total = 0
    with engine.begin() as conn:
        for table in ("refresh_log", "keepalive_log"):
            res = conn.execute(text(f"DELETE FROM {table} WHERE ts < :c"), {"c": cutoff})
            total += res.rowcount or 0
    if total:
        logger.info("Log-opschoning: %d regels ouder dan %d dagen verwijderd", total, days)
    return total


def prune_debug_files(days: int = 30) -> int:
    """Verwijder debug-dumps (screenshots/HTML met saldi) ouder dan `days`."""
    if not DEBUG_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for p in DEBUG_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("Debug-opschoning: %d bestanden ouder dan %d dagen verwijderd", removed, days)
    return removed


def write_keepalive_log(status: str, message: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO keepalive_log (ts, status, message) VALUES (:ts, :st, :msg)"),
            {"ts": now_iso(), "st": status, "msg": message},
        )


def last_ok_refresh_dt() -> datetime | None:
    """Tijdstip van de laatste geslaagde refresh, of None."""
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT ts FROM refresh_log WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        )).first()
    if not row:
        return None
    try:
        dt = datetime.fromisoformat(row[0])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def consecutive_failed_refreshes() -> int:
    """Aantal opeenvolgende 'failed' refreshes aan het eind van de log
    ('skipped'/'session_expired' tellen niet mee)."""
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT status FROM refresh_log WHERE status IN ('ok', 'failed') "
            "ORDER BY id DESC LIMIT 25"
        )).all()
    streak = 0
    for (status,) in rows:
        if status == "failed":
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
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
    return {r["account_number"]: to_float(r["value_eur"]) for r in rows}


def snapshot_values_at(dt: datetime) -> dict[str, float]:
    """Laatst bekende waarde per rekening op of vóór tijdstip `dt`."""
    iso = dt.astimezone(timezone.utc).isoformat()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, value_eur
            FROM accounts_snapshot
            WHERE id IN (
                SELECT MAX(id) FROM accounts_snapshot
                WHERE ts <= :ts GROUP BY account_number
            )
        """), {"ts": iso}).mappings().all()
    return {r["account_number"]: to_float(r["value_eur"]) for r in rows}


def snapshot_values_first_from(dt: datetime) -> dict[str, float]:
    """Eerste bekende waarde per rekening op of ná tijdstip `dt` (voor
    rekeningen die pas ná een peildatum zijn begonnen)."""
    iso = dt.astimezone(timezone.utc).isoformat()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, value_eur
            FROM accounts_snapshot
            WHERE id IN (
                SELECT MIN(id) FROM accounts_snapshot
                WHERE ts >= :ts GROUP BY account_number
            )
        """), {"ts": iso}).mappings().all()
    return {r["account_number"]: to_float(r["value_eur"]) for r in rows}


def account_labels() -> dict[str, str]:
    """Meest recente label per rekening."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, label
            FROM accounts_snapshot
            WHERE id IN (SELECT MAX(id) FROM accounts_snapshot GROUP BY account_number)
        """)).mappings().all()
    return {r["account_number"]: r["label"] for r in rows}


def deposits_sum_between(start: datetime, end: datetime) -> float:
    """Som van de inleg met start <= ts < end."""
    a = start.astimezone(timezone.utc).isoformat()
    b = end.astimezone(timezone.utc).isoformat()
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT COALESCE(SUM(amount_eur), 0) FROM deposits WHERE ts >= :a AND ts < :b"
        ), {"a": a, "b": b}).first()
    return to_float(row[0]) if row else 0.0


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
        pt = {"ts": r["ts"], "value_eur": to_float(r["value_eur"])}
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
# Deposits (inleg)
# ---------------------------------------------------------------------------
def get_deposits() -> dict[str, float]:
    """Return {account_number: total_deposited_eur}"""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT account_number, SUM(amount_eur) as total
            FROM deposits GROUP BY account_number
        """)).mappings().all()
    return {r["account_number"]: to_float(r["total"]) for r in rows}


def load_deposits() -> list[dict]:
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, ts, account_number, label, amount_eur, note
            FROM deposits ORDER BY ts DESC
        """)).mappings().all()
    return [dict(r) for r in rows]


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
            "amount_eur":     to_float(r["amount_eur"]),
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
                amt = to_float(e.get("amount_eur", 0))
                note = (e.get("note") or "").strip() or None
                # amt mag negatief zijn (onttrekking), alleen 0 is ongeldig
                if not ts or not acc or amt == 0:
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
# Cookie / session summary (voor de sessie-pagina)
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
                remaining = fmt_timedelta(float(exp) - now_ts)
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
