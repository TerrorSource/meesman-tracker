"""Rooktests tegen de draaiende app (TestClient): routes, import, inleg, config, health."""
import io
import json
import re


GET_ROUTES = ["/", "/health", "/config", "/deposits", "/import", "/session",
              "/api/sensors", "/api/accounts", "/export.json", "/deposits.json",
              "/export.csv", "/deposits.csv"]


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
    export = {"accounts": [{"account_number": "22404586", "label": "Beleggingen",
              "history": [{"ts": "2026-01-01T00:00:00+00:00", "value_eur": 100.0},
                          {"ts": "2026-02-01T00:00:00+00:00", "value_eur": 110.0},
                          {"ts": "2026-03-01T00:00:00+00:00", "value_eur": 120.0}]}]}
    for _ in range(2):   # tweede keer alles overgeslagen (INSERT OR IGNORE)
        f = ("export.json", io.BytesIO(json.dumps(export).encode()), "application/json")
        assert client.post("/import", files=[("files", f)]).status_code == 200
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


def test_config_validation_totp_and_time(client):
    client.post("/config/generate-key", follow_redirects=False)

    r = client.post("/config/save", data={"username": "x", "totp_secret": "dit-is-geen-base32!!!"},
                    follow_redirects=False)
    assert "bad_totp" in r.headers["location"]

    r = client.post("/config/save", data={"username": "x", "refresh_time": "25:99"}, follow_redirects=False)
    assert "bad_time" in r.headers["location"]

    r = client.post("/config/save", data={"username": "x", "totp_secret": "JBSWY3DPEHPK3PXP",
                                          "mfa_mode": "totp", "refresh_time": "7:30"}, follow_redirects=False)
    assert "saved=1" in r.headers["location"]

    from app.config_store import load_config
    assert load_config()["refresh_time"] == "07:30"   # genormaliseerd


def test_refresh_trigger_cron_vs_interval():
    from app.service_refresh import build_refresh_trigger, refresh_stale_threshold_hours
    trig, desc = build_refresh_trigger({"refresh_time": "07:30", "refresh_hours": 24})
    assert type(trig).__name__ == "CronTrigger" and "07:30" in desc
    trig, desc = build_refresh_trigger({"refresh_time": "", "refresh_hours": 6})
    assert type(trig).__name__ == "IntervalTrigger" and "6" in desc
    assert refresh_stale_threshold_hours({"refresh_time": "07:30"}) == 26.0
    assert refresh_stale_threshold_hours({"refresh_time": "", "refresh_hours": 6}) == 6.0


def test_monthly_summary_builds(client):
    from datetime import datetime
    from app.core import LOCAL_TZ
    from app.telegram import build_monthly_summary
    # Data uit de import-test: 100 (jan) → 110 (feb) → 120 (mrt); overzicht per 1 april
    msg = build_monthly_summary(datetime(2026, 4, 1, 8, 0, tzinfo=LOCAL_TZ))
    assert msg and "maart 2026" in msg and "Beleggingen" in msg
    assert "Sinds 1 januari" in msg


def test_csv_dutch_format(client):
    txt = client.get("/export.csv").text
    assert "datum_utc;rekeningnummer;naam;waarde_eur" in txt and "29869,81" in txt


def test_dashboard_has_controls_and_local_time(client):
    html = client.get("/").text
    assert 'id="baseline-date"' in html and 'id="period-buttons"' in html
    assert "vtest" in html


def test_fernet_roundtrip_after_key_generation(client):
    from app.security import decrypt_str, encrypt_str
    token = encrypt_str("geheim-123 €")
    assert token != "geheim-123 €"
    assert decrypt_str(token) == "geheim-123 €"
