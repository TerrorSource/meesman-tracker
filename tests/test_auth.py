"""v10: Basic Auth, API-token, proxy-tolerante CSRF-guard, master key uit env, MFA-code wissen."""
import base64

import pytest

from app.auth import auth


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setattr(auth, "user", "marc")
    monkeypatch.setattr(auth, "password", "geheim")
    monkeypatch.setattr(auth, "api_token", "tok-123")
    monkeypatch.setattr(auth, "fail_delay", 0)
    yield


def test_open_without_env(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/sensors").status_code == 200
    assert "niet beveiligd" in client.get("/config").text


def test_ui_redirects_to_login_and_basic_still_works(client, secured):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert client.get("/", headers=_basic("marc", "geheim")).status_code == 200
    assert client.get("/", headers=_basic("marc", "fout"), follow_redirects=False).status_code == 303
    assert client.get("/config", headers=_basic("marc", "geheim")).text.count("Basic Auth actief") == 1
    # formulieren ook dicht (POST zonder sessie → 401, geen redirect)
    assert client.post("/config/save", data={"username": "x"}).status_code == 401
    # open paden
    assert client.get("/health").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/login").status_code == 200


def test_login_form_session_and_logout(client, secured):
    html = client.get("/login?next=/config").text
    assert 'autocomplete="username"' in html and 'autocomplete="current-password"' in html
    assert 'name="next" value="/config"' in html

    r = client.post("/login", data={"username": "marc", "password": "fout", "next": "/config"})
    assert r.status_code == 401 and "Onjuiste" in r.text

    r = client.post("/login", data={"username": "marc", "password": "geheim", "next": "/config"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config"
    assert "meesman_session=" in r.headers.get("set-cookie", "") and "HttpOnly" in r.headers["set-cookie"]

    # cookie-jar van de client → nu ingelogd
    r = client.get("/config")
    assert r.status_code == 200 and "Uitloggen" in r.text
    assert client.get("/api/sensors").status_code == 200          # sessie geldt ook voor de API
    assert client.get("/login", follow_redirects=False).status_code == 303  # al ingelogd → door

    # open redirect voorkomen
    r = client.post("/login", data={"username": "marc", "password": "geheim", "next": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"

    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_token_validation(secured):
    tok = auth.make_session_token()
    assert auth.session_valid(tok) is True
    assert auth.session_valid(tok[:-1] + ("0" if tok[-1] != "0" else "1")) is False   # vervalste handtekening
    assert auth.session_valid("123.abc") is False and auth.session_valid(None) is False
    exp, sig = tok.split(".", 1)
    assert auth.session_valid(f"{int(exp) - 10 * 86400 * 365}.{sig}") is False        # verlopen


def test_api_token_and_basic_on_machine_endpoints(client, secured):
    assert client.get("/api/sensors").status_code == 401
    assert client.get("/api/sensors", headers={"Authorization": "Bearer tok-123"}).status_code == 200
    assert client.get("/api/sensors", headers={"X-API-Token": "tok-123"}).status_code == 200
    assert client.get("/api/sensors", headers={"Authorization": "Bearer fout"}).status_code == 401
    assert client.get("/export.json", headers={"Authorization": "Bearer tok-123"}).status_code == 200
    assert client.get("/deposits.json", headers=_basic("marc", "geheim")).status_code == 200
    # token geeft géén toegang tot de UI
    assert client.get("/config", headers={"Authorization": "Bearer tok-123"}, follow_redirects=False).status_code == 303


def test_only_token_set_keeps_ui_open(client, monkeypatch):
    monkeypatch.setattr(auth, "user", "")
    monkeypatch.setattr(auth, "password", "")
    monkeypatch.setattr(auth, "api_token", "tok-123")
    monkeypatch.setattr(auth, "fail_delay", 0)
    assert client.get("/").status_code == 200
    assert client.get("/api/sensors").status_code == 401
    assert client.get("/api/sensors", headers={"X-API-Token": "tok-123"}).status_code == 200


def test_csrf_guard_respects_forwarded_host(client):
    hdrs = {"origin": "https://tracker.example.org", "host": "testserver",
            "x-forwarded-host": "tracker.example.org"}
    r = client.post("/deposits/add", data={"account_number": "", "entry_date": "2026-01-01", "amount_eur": "1,00"},
                    headers=hdrs)
    assert r.status_code != 403          # zelfde host achter de proxy → toegestaan
    r = client.post("/deposits/add", data={"account_number": "", "entry_date": "2026-01-01", "amount_eur": "1,00"},
                    headers={"origin": "https://evil.example", "host": "testserver", "x-forwarded-host": "tracker.example.org"})
    assert r.status_code == 403


def test_security_headers(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_master_key_from_env(client, monkeypatch):
    from cryptography.fernet import Fernet
    from app.security import decrypt_str, encrypt_str, get_or_create_master_key, master_key_source
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("MASTER_KEY", key)
    assert master_key_source() == "env" and get_or_create_master_key() == key
    assert decrypt_str(encrypt_str("test €")) == "test €"
    assert "uit omgevingsvariabele" in client.get("/config").text
    monkeypatch.delenv("MASTER_KEY")
    assert master_key_source() in ("config", "none")


def test_clear_manual_mfa_code(client):
    from app.config_store import load_config, save_config
    from app.security import encrypt_str
    from app.service_refresh import clear_manual_mfa_code
    client.post("/config/generate-key", follow_redirects=False)
    cfg = load_config()
    cfg["manual_mfa_code_enc"] = encrypt_str("123456")
    save_config(cfg)
    assert clear_manual_mfa_code() is True
    assert load_config()["manual_mfa_code_enc"] == ""
    assert clear_manual_mfa_code() is False   # niets meer te wissen
