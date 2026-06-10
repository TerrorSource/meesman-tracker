"""Routes: dashboard, healthcheck, handmatige refresh en sessie-pagina."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from .config_store import load_config
from .core import (
    APP_VERSION_FULL,
    SESSION_STATE_PATH,
    engine,
    logger,
    now_iso,
    templates,
    to_float,
)
from .service_refresh import refresh_once
from .store import get_deposits, read_cookie_dump_summary, write_export_json

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
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
        val = to_float(r["value_eur"])  # SQLite kan tekst of komma-notatie teruggeven (legacy rijen)

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
        # '</' escapen zodat een label nooit uit het <script>-blok kan breken
        "payload_json": json.dumps(payload).replace("</", "<\\/"),
        "last_refresh": dict(last) if last else None,
        "export_path":  "/export.json",
    })


@router.get("/health")
def health():
    """Healthcheck voor Docker/NAS."""
    return JSONResponse({"status": "ok", "version": APP_VERSION_FULL, "time": now_iso()})


@router.post("/datapoints/delete")
async def datapoint_delete(account_number: str = Form(...), ts: str = Form(...)):
    """Verwijder één datapunt (snapshot-rij) — aangeroepen vanaf de
    Wijzigingen-tabel op het dashboard."""
    with engine.begin() as conn:
        res = conn.execute(
            text("DELETE FROM accounts_snapshot WHERE account_number = :n AND ts = :ts"),
            {"n": account_number, "ts": ts},
        )

    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Datapunt {account_number} @ {ts} niet gevonden")

    logger.info("Datapunt verwijderd: %s @ %s", account_number, ts)
    try:
        write_export_json()
    except Exception as e:
        logger.warning("export.json herbouwen na verwijderen datapunt mislukt: %s", e)

    return JSONResponse({"deleted": res.rowcount})


@router.post("/refresh-now")
async def refresh_now():
    logger.info("Handmatige refresh gestart.")
    await refresh_once()
    return RedirectResponse(url="/", status_code=303)


@router.get("/session", response_class=HTMLResponse)
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
