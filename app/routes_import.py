"""Routes: import van export.json/deposits.json en handmatige datapunten."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from .core import engine, logger, parse_form_amount, templates, to_float
from .store import write_deposits_json, write_export_json

router = APIRouter()


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return templates.TemplateResponse("import.html", {"request": request})


@router.post("/import")
async def import_post(request: Request, files: list[UploadFile] = File(...)):
    """
    Accept one or more export.json files.
    Deduplicates on (ts, account_number) via the unique index — duplicates are skipped.
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

                    # De unieke index op (account_number, ts) vangt duplicaten af
                    res = conn.execute(text("""
                        INSERT OR IGNORE INTO accounts_snapshot (ts, account_number, label, value_eur)
                        VALUES (:ts, :n, :l, :v)
                    """), {"ts": ts, "n": acc_number, "l": label, "v": to_float(value_eur)})

                    if res.rowcount:
                        inserted += 1
                    else:
                        skipped += 1

        results.append({"file": fname, "error": None, "inserted": inserted, "skipped": skipped})
        logger.info("Import %s: %d ingevoerd, %d overgeslagen", fname, inserted, skipped)

    # Rebuild export.json after import
    if any(r["inserted"] > 0 for r in results):
        try:
            write_export_json()
        except Exception as e:
            logger.warning("export.json herbouwen na import mislukt: %s", e)

    total_inserted = sum(r["inserted"] for r in results)
    total_skipped  = sum(r["skipped"]  for r in results)

    return templates.TemplateResponse("import.html", {
        "request":        request,
        "results":        results,
        "total_inserted": total_inserted,
        "total_skipped":  total_skipped,
    })


@router.post("/import/manual")
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
        value_eur_float = parse_form_amount(value_eur)
    except (ValueError, AttributeError):
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
        """), {"ts": ts, "n": acc_number, "l": label.strip() or acc_number, "v": value_eur_float})

    try:
        write_export_json()
    except Exception as e:
        logger.warning("export.json herbouwen na handmatig datapunt mislukt: %s", e)

    logger.info("Handmatig datapunt toegevoegd: %s %s € %.2f", acc_number, ts, value_eur_float)

    return templates.TemplateResponse("import.html", {
        "request":         request,
        "manual_inserted": 1,
        "manual_ts":       ts,
        "manual_account":  acc_number,
        "manual_value":    value_eur_float,
    })


@router.post("/import/deposits")
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
                amt = to_float(e.get("amount_eur", 0))
                note = (e.get("note") or "").strip() or None

                # amt mag negatief zijn (onttrekking), alleen 0 is ongeldig
                if not ts or not acc or amt == 0:
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
        except Exception as e:
            logger.warning("deposits.json herbouwen na import mislukt: %s", e)

    total_inserted = sum(r["inserted"] for r in results)
    total_skipped  = sum(r["skipped"]  for r in results)

    return templates.TemplateResponse("import.html", {
        "request":                  request,
        "dep_results":              results,
        "dep_total_inserted":       total_inserted,
        "dep_total_skipped":        total_skipped,
    })
