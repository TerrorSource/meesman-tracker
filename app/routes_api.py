"""Routes: JSON/CSV-exports en de Home Assistant sensor-API."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from .core import DEPOSITS_PATH, EXPORT_PATH, engine, to_float
from .store import write_deposits_json, write_export_json

router = APIRouter()


def _csv_response(header: list[str], rows: list[list], filename: str) -> Response:
    """NL-vriendelijke CSV: puntkomma als scheidingsteken, komma als decimaalteken."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    w.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _nl_num(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


@router.get("/export.json")
def export_json():
    if not EXPORT_PATH.exists():
        try:
            write_export_json()
        except Exception:
            raise HTTPException(status_code=404, detail="export.json nog niet beschikbaar")
    return FileResponse(str(EXPORT_PATH), media_type="application/json", filename="export.json")


@router.get("/export.csv")
def export_csv():
    """Volledige saldohistorie als CSV (voor Excel/Numbers)."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT ts, account_number, label, value_eur
            FROM accounts_snapshot ORDER BY account_number, ts
        """)).mappings().all()
    return _csv_response(
        ["datum_utc", "rekeningnummer", "naam", "waarde_eur"],
        [[r["ts"], r["account_number"], r["label"], _nl_num(to_float(r["value_eur"]))] for r in rows],
        "meesman-export.csv",
    )


@router.get("/deposits.csv")
def deposits_csv():
    """Alle inleggen als CSV (voor Excel/Numbers)."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT ts, account_number, label, amount_eur, note
            FROM deposits ORDER BY account_number, ts
        """)).mappings().all()
    return _csv_response(
        ["datum_utc", "rekeningnummer", "naam", "bedrag_eur", "omschrijving"],
        [[r["ts"], r["account_number"], r["label"], _nl_num(to_float(r["amount_eur"])), r["note"] or ""] for r in rows],
        "meesman-inleg.csv",
    )


@router.get("/deposits.json")
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


@router.get("/api/sensors")
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
            "value_eur":      to_float(r["value_eur"]),
            "last_updated":   r["ts"],
        }
        for r in rows
    ]
    total = sum(a["value_eur"] for a in accounts)

    return JSONResponse({"total": round(total, 2), "accounts": accounts})


@router.get("/api/sensors/{account_number}")
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
        "value_eur":      to_float(row["value_eur"]),
        "last_updated":   row["ts"],
    })


@router.get("/api/accounts")
def api_accounts():
    """Returns known account numbers + labels for the manual entry form."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT account_number, label
            FROM accounts_snapshot
            ORDER BY account_number
        """)).mappings().all()
    return JSONResponse([{"account_number": r["account_number"], "label": r["label"]} for r in rows])
