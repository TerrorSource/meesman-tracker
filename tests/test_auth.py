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


def test_ui_requires_basic_auth(client, secured):
    r = client.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/", headers=_basic("marc", "geheim")).status_code == 200
    assert client.get("/", headers=_basic("marc", "fout")).status_code == 401
    assert client.get("/config", headers=_basic("marc", "geheim")).text.count("Basic Auth actief") == 1
    # formulieren ook dicht
    assert client.post("/config/save", data={"username": "x"}).status_code == 401
    # open paden
    assert client.get("/health").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_api_token_and_basic_on_machine_endpoints(client, secured):
    assert client.get("/api/sensors").status_code == 401
    assert client.get("/api/sensors", headers={"Authorization": "Bearer tok-123"}).status_code == 200
    assert client.get("/api/sensors", headers={"X-API-Token": "tok-123"}).status_code == 200
    assert client.get("/api/sensors", headers={"Authorization": "Bearer fout"}).status_code == 401
    assert client.get("/export.json", headers={"Authorization": "Bearer tok-123"}).status_code == 200
    assert client.get("/deposits.json", headers=_basic("marc", "geheim")).status_code == 200
    # token geeft géén toegang tot de UI
    assert client.get("/config", headers={"Authorization": "Bearer tok-123"}).status_code == 401


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
