"""Rooktests tegen de draaiende app (TestClient): routes, import, inleg, config, health, archiveren, backups."""
import asyncio
import io
import json
import re
from pathlib import Path

import pytest

GET_ROUTES = ["/", "/health", "/config", "/deposits", "/import", "/session",
              "/api/sensors", "/api/accounts", "/export.json", "/deposits.json",
              "/export.csv", "/deposits.csv"]

FIXTURE = Path(__file__).parent / "fixtures" / "meesman_overview.html"


def _import(client, accounts):
    f = ("export.json", io.BytesIO(json.dumps({"accounts": accounts}).encode()), "application/json")
    r = client.post("/import", files=[("files", f)])
    assert r.status_code == 200
    return r


def test_all_get_routes(client):
    for path in GET_ROUTES:
        assert client.get(path).status_code == 200, path


def test_health_reports_version_and_degraded_without_data(client):
    h = client.get("/health").json()
    assert h["version"] == "vtest"
    assert h["status"] == "degraded"        # nog nooit een geslaagde refresh
    assert h["last_ok_refresh"] is None


def test_csrf_guard_blocks_cross_origin_post(client):
    r = client.post("/deposits/add",
                    data={"account_number": "1", "entry_date": "2026-01-01", "amount_eur": "1,00"},
                    headers={"origin": "http://evil.example", "host": "testserver"})
    assert r.status_code == 403


def test_import_and_dedupe(client):
    export = [{"account_number": "22404586", "label": "Beleggingen",
               "history": [{"ts": "2026-01-01T00:00:00+00:00", "value_eur": 100.0},
                           {"ts": "2026-02-01T00:00:00+00:00", "value_eur": 110.0},
                           {"ts": "2026-03-01T00:00:00+00:00", "value_eur": 120.0}]}]
    for _ in range(2):   # tweede keer alles overgeslagen (INSERT OR IGNORE)
        _import(client, export)
    s = client.get("/api/sensors").json()
    assert s["total"] == 120.0 and len(s["accounts"]) == 1


def test_manual_datapoint_stored_as_float(client):
    r = client.post("/import/manual", data={"account_number": "22404586", "entry_date": "2026-03-05",
                                            "entry_time": "12:00", "value_eur": "29.869,81"})
    assert r.status_code == 200
    assert client.get("/api/sensors/22404586").json()["value_eur"] == 29869.81


def test_datapoint_delete(client):
    r = client.post("/datapoints/delete", data={"account_number": "22404586", "ts": "2026-02-01T00:00:00+00:00"})
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    r = client.post("/datapoints/delete", data={"account_number": "22404586", "ts": "2026-02-01T00:00:00+00:00"})
    assert r.status_code == 404


def test_deposits_add_update_delete(client):
    r = client.post("/deposits/add", data={"account_number": "22404586", "entry_date": "2026-01-15",
                                           "amount_eur": "500,00", "note": "test"}, follow_redirects=False)
    assert r.status_code == 303
    dep_id = re.search(r'data-id="(\d+)"', client.get("/deposits").text).group(1)

    r = client.post(f"/deposits/update/{dep_id}", data={"account_number": "22404586", "entry_date": "2026-01-16",
                                                        "entry_time": "10:30", "amount_eur": "750,50", "note": "aangepast"},
                    follow_redirects=False)
    assert r.status_code == 303
    deps = client.get("/deposits.json").json()["deposits"]
    assert deps[0]["amount_eur"] == 750.5 and deps[0]["note"] == "aangepast"

    assert "niet gevonden" in client.post("/deposits/update/99999", data={
        "account_number": "22404586", "entry_date": "2026-01-16", "amount_eur": "1,00"}).text

    assert client.post(f"/deposits/delete/{dep_id}", follow_redirects=False).status_code == 303
    assert client.get("/deposits.json").json()["deposits"] == []


def test_config_validation_and_new_settings(client):
    client.post("/config/generate-key", follow_redirects=False)

    r = client.post("/config/save", data={"username": "x", "totp_secret": "dit-is-geen-base32!!!"},
                    follow_redirects=False)
    assert "bad_totp" in r.headers["location"]

    r = client.post("/config/save", data={"username": "x", "refresh_time": "25:99"}, follow_redirects=False)
    assert "bad_time" in r.headers["location"]

    r = client.post("/config/save", data={
        "username": "x", "totp_secret": "JBSWY3DPEHPK3PXP", "mfa_mode": "totp",
        "refresh_time": "7:30", "refresh_days": "mon-sat",
        "notify_min_eur": "10,5", "notify_min_pct": "0.25", "fail_alert_threshold": "2",
        "weekly_summary": "1", "backup_keep": "7",
        "sel_accounts_row_selector": "table.custom tr",
    }, follow_redirects=False)
    assert "saved=1" in r.headers["location"]

    from app.config_store import DEFAULT_SELECTORS, load_config
    cfg = load_config()
    assert cfg["refresh_time"] == "07:30" and cfg["refresh_days"] == "mon-sat"
    assert cfg["notify_min_eur"] == 10.5 and cfg["notify_min_pct"] == 0.25
    assert cfg["fail_alert_threshold"] == 2 and cfg["weekly_summary"] is True
    assert cfg["monthly_summary"] is False          # checkbox niet meegestuurd = uit
    assert cfg["backup_keep"] == 7
    assert cfg["selectors"]["accounts_row_selector"] == "table.custom tr"
    assert cfg["selectors"]["login_user_selector"] == DEFAULT_SELECTORS["login_user_selector"]  # leeg → standaard

    # selectors terugzetten
    assert client.post("/config/selectors/reset", follow_redirects=False).status_code == 303
    assert load_config()["selectors"] == DEFAULT_SELECTORS
    # config-pagina toont de nieuwe secties
    html = client.get("/config").text
    assert "Meldingen" in html and "Scraper-selectors" in html and 'name="refresh_days"' in html


def test_refresh_trigger_cron_vs_interval_and_days():
    from app.service_refresh import build_refresh_trigger, refresh_stale_threshold_hours
    trig, desc = build_refresh_trigger({"refresh_time": "07:30", "refresh_hours": 24, "refresh_days": "daily"})
    assert type(trig).__name__ == "CronTrigger" and "07:30" in desc and "dagelijks" in desc
    trig, desc = build_refresh_trigger({"refresh_time": "07:30", "refresh_days": "mon-fri"})
    assert "ma–vr" in desc and "mon-fri" in str(trig)
    trig, desc = build_refresh_trigger({"refresh_time": "", "refresh_hours": 6})
    assert type(trig).__name__ == "IntervalTrigger" and "6" in desc
    assert refresh_stale_threshold_hours({"refresh_time": "07:30", "refresh_days": "daily"}) == 26.0
    assert refresh_stale_threshold_hours({"refresh_time": "07:30", "refresh_days": "mon-fri"}) == 74.0
    assert refresh_stale_threshold_hours({"refresh_time": "", "refresh_hours": 6}) == 6.0


def test_monthly_and_weekly_summary_build(client):
    from datetime import datetime
    from app.core import LOCAL_TZ
    from app.telegram import build_monthly_summary, build_weekly_summary
    # Data: 100 (1 jan) → 120 (1 mrt) → 29869.81 (5 mrt); overzicht per 1 april
    msg = build_monthly_summary(datetime(2026, 4, 1, 8, 0, tzinfo=LOCAL_TZ))
    assert msg and "maart 2026" in msg and "Beleggingen" in msg and "Sinds 1 januari" in msg
    wk = build_weekly_summary(datetime(2026, 3, 9, 8, 0, tzinfo=LOCAL_TZ))
    assert wk and "weekoverzicht" in wk and "02-03 t/m 08-03-2026" in wk


def test_yearly_returns(client):
    from app.store import yearly_returns
    years = yearly_returns()
    assert len(years) == 1 and years[0]["year"] == 2026 and years[0]["partial"] is True
    y = years[0]
    assert y["begin"] == 100.0 and y["end"] == 29869.81
    assert abs(y["ret"] - (29869.81 - 100.0)) < 0.01
    html = client.get("/").text
    assert "Rendement per kalenderjaar" in html and "(lopend)" in html


def test_archive_flow(client):
    _import(client, [{"account_number": "25110311", "label": "Pensioen",
                      "history": [{"ts": "2026-03-01T00:00:00+00:00", "value_eur": 500.0}]}])
    assert client.get("/api/sensors").json()["total"] == 29869.81 + 500.0

    assert client.post("/accounts/25110311/archive", follow_redirects=False).status_code == 303
    assert "Pensioen" not in client.get("/").text
    assert "Pensioen" in client.get("/?archived=1").text
    s = client.get("/api/sensors").json()
    assert s["total"] == 29869.81 and [a["account_number"] for a in s["accounts"]] == ["22404586"]
    assert client.get("/api/sensors/25110311").json()["archived"] is True

    assert client.post("/accounts/25110311/unarchive", follow_redirects=False).status_code == 303
    assert "Pensioen" in client.get("/").text
    assert client.get("/api/sensors").json()["total"] == 29869.81 + 500.0


def test_missing_accounts_detection(client):
    from app.store import update_missing_accounts
    newly, back = update_missing_accounts({"22404586"})            # Pensioen ontbreekt in de scrape
    assert newly == ["25110311"] and back == []
    newly, back = update_missing_accounts({"22404586"})            # tweede keer: geen nieuwe melding
    assert newly == [] and back == []
    newly, back = update_missing_accounts({"22404586", "25110311"})  # weer terug
    assert newly == [] and back == ["25110311"]


def test_api_sensors_freshness_fields(client):
    s = client.get("/api/sensors").json()
    assert s["status"] == "degraded" and s["last_ok_refresh"] is None and "last_ok_age_hours" in s


def test_backup_database(client):
    from app.core import BACKUP_DIR
    from app.store import backup_database, backup_exists_today
    for _ in range(3):
        p = backup_database(keep=2)
        assert p.exists() and p.stat().st_size > 0
    assert len(list(BACKUP_DIR.glob("app-*.db"))) == 2
    assert backup_exists_today() is True
    # de kopie is een geldige SQLite-database met dezelfde tabellen
    import sqlite3
    con = sqlite3.connect(p)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"accounts_snapshot", "deposits", "refresh_log", "account_settings"} <= tables


def test_fernet_roundtrip_after_key_generation(client):
    from app.security import decrypt_str, encrypt_str
    token = encrypt_str("geheim-123 €")
    assert token != "geheim-123 €"
    assert decrypt_str(token) == "geheim-123 €"


def test_csv_dutch_format(client):
    txt = client.get("/export.csv").text
    assert "datum_utc;rekeningnummer;naam;waarde_eur" in txt and "29869,81" in txt


def test_dashboard_has_controls_and_local_time(client):
    html = client.get("/").text
    assert 'id="baseline-date"' in html and 'id="period-buttons"' in html
    assert "vtest" in html and 'href="/session"' in html


def test_parse_accounts_from_fixture():
    """Regressietest van de tabel-parsing tegen een HTML-fixture (offline)."""
    from app.config_store import DEFAULT_SELECTORS
    from app.scraper import parse_accounts_from_page
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:  # geen browser geïnstalleerd → overslaan
                pytest.skip(f"Chromium niet beschikbaar: {str(e)[:80]}")
            page = await browser.new_page()
            await page.goto(FIXTURE.resolve().as_uri())
            result = await parse_accounts_from_page(page, DEFAULT_SELECTORS)
            await browser.close()
            return result

    accounts, primary_ok = asyncio.run(run())
    assert primary_ok is True
    assert [(a.account_number, a.label, a.value_eur) for a in accounts] == [
        ("22404586", "Beleggingen", 33192.87),
        ("25110311", "Pensioen", 57417.17),
    ]   # de lege totaalregel is genegeerd


def test_refresh_log_duration_and_history_page(client):
    """Laatste test: schrijft een 'ok'-regel, dus health wordt hierna 'ok'."""
    from app.store import recent_refresh_log, write_refresh_log
    write_refresh_log("failed", 0, "x" * 800, 3.2)
    write_refresh_log("ok", 2, None, 12.3)
    rows = recent_refresh_log(5)
    assert rows[0]["status"] == "ok" and rows[0]["duration_s"] == 12.3
    assert len(rows[1]["message"]) <= 505             # ingekort tot 500 + ' …'
    assert client.get("/health").json()["status"] == "ok"
    html = client.get("/session").text
    assert "Refresh-geschiedenis" in html and "12.3 s" in html and "Uitgeschakeld in TOTP-modus" in html
