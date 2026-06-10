"""Routes: inleg (deposits) — toevoegen, bewerken, verwijderen."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from .core import engine, logger, parse_form_amount, templates
from .store import load_deposits, write_deposits_json

router = APIRouter()


def _error_page(request: Request, message: str):
    return templates.TemplateResponse("deposits.html", {
        "request": request, "error": message, "deposits": load_deposits(),
    })


def _parse_deposit_form(request: Request, account_number: str, entry_date: str,
                        entry_time: str, amount_eur: str):
    """Valideer gedeelde formuliervelden. Returnt (acc, ts, amount) of een error-response."""
    acc_number = account_number.strip()
    if not acc_number:
        return _error_page(request, "Rekeningnummer is verplicht.")

    try:
        dt = datetime.fromisoformat(f"{entry_date}T{entry_time}:00").replace(tzinfo=timezone.utc)
        ts = dt.isoformat()
    except ValueError:
        return _error_page(request, "Ongeldige datum of tijd.")

    try:
        amount_float = parse_form_amount(amount_eur)
    except (ValueError, AttributeError):
        return _error_page(request, f"Ongeldig bedrag '{amount_eur}'. Gebruik komma: bijv. 4987,50")

    return acc_number, ts, amount_float


def _lookup_label(conn, acc_number: str) -> str:
    row = conn.execute(text("""
        SELECT label FROM accounts_snapshot
        WHERE account_number = :n ORDER BY id DESC LIMIT 1
    """), {"n": acc_number}).first()
    return row[0] if row else acc_number


@router.get("/deposits", response_class=HTMLResponse)
def deposits_page(request: Request):
    return templates.TemplateResponse("deposits.html", {
        "request": request,
        "deposits": load_deposits(),
    })


@router.post("/deposits/add")
async def deposits_add(
    request:        Request,
    account_number: str   = Form(...),
    entry_date:     str   = Form(...),
    entry_time:     str   = Form("00:00"),
    amount_eur:     str   = Form(...),
    note:           str   = Form(""),
):
    parsed = _parse_deposit_form(request, account_number, entry_date, entry_time, amount_eur)
    if not isinstance(parsed, tuple):
        return parsed
    acc_number, ts, amount_float = parsed

    with engine.begin() as conn:
        label = _lookup_label(conn, acc_number)
        conn.execute(text("""
            INSERT INTO deposits (ts, account_number, label, amount_eur, note)
            VALUES (:ts, :n, :l, :v, :note)
        """), {"ts": ts, "n": acc_number, "l": label, "v": amount_float, "note": note.strip() or None})

    logger.info("Inleg toegevoegd: %s %s € %.2f", acc_number, ts, amount_float)
    try:
        write_deposits_json()
    except Exception as e:
        logger.warning("deposits.json bijwerken mislukt: %s", e)
    return RedirectResponse(url="/deposits?saved=1", status_code=303)


@router.post("/deposits/update/{deposit_id}")
async def deposits_update(
    request:        Request,
    deposit_id:     int,
    account_number: str   = Form(...),
    entry_date:     str   = Form(...),
    entry_time:     str   = Form("00:00"),
    amount_eur:     str   = Form(...),
    note:           str   = Form(""),
):
    parsed = _parse_deposit_form(request, account_number, entry_date, entry_time, amount_eur)
    if not isinstance(parsed, tuple):
        return parsed
    acc_number, ts, amount_float = parsed

    with engine.begin() as conn:
        label = _lookup_label(conn, acc_number)
        res = conn.execute(text("""
            UPDATE deposits
            SET ts = :ts, account_number = :n, label = :l, amount_eur = :v, note = :note
            WHERE id = :id
        """), {"id": deposit_id, "ts": ts, "n": acc_number, "l": label,
               "v": amount_float, "note": note.strip() or None})

    if res.rowcount == 0:
        return _error_page(request, f"Inleg met id {deposit_id} niet gevonden.")

    logger.info("Inleg bijgewerkt: id=%d %s %s € %.2f", deposit_id, acc_number, ts, amount_float)
    try:
        write_deposits_json()
    except Exception as e:
        logger.warning("deposits.json bijwerken mislukt: %s", e)
    return RedirectResponse(url="/deposits?updated=1", status_code=303)


@router.post("/deposits/delete/{deposit_id}")
async def deposits_delete(deposit_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM deposits WHERE id = :id"), {"id": deposit_id})
    logger.info("Inleg verwijderd: id=%d", deposit_id)
    try:
        write_deposits_json()
    except Exception as e:
        logger.warning("deposits.json bijwerken mislukt: %s", e)
    return RedirectResponse(url="/deposits?deleted=1", status_code=303)
