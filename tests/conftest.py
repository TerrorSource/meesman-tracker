"""
Test-setup: alle data-paden naar een tijdelijke map wijzen VÓÓRDAT de app
geïmporteerd wordt (app.core maakt bij import de engine + tabellen aan).
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="meesman-test-"))
os.environ.update({
    "DATA_DIR":           str(_TMP),
    "DB_PATH":            str(_TMP / "app.db"),
    "CONFIG_PATH":        str(_TMP / "config.yaml"),
    "EXPORT_PATH":        str(_TMP / "export.json"),
    "SESSION_STATE_PATH": str(_TMP / "session.json"),
    "COOKIES_DUMP_PATH":  str(_TMP / "cookies.json"),
    "DEPOSITS_PATH":      str(_TMP / "deposits.json"),
    "DEBUG_DIR":          str(_TMP / "debug"),
    "TZ":                 "Europe/Amsterdam",
    "APP_VERSION":        "vtest",
    "SELF_RESTART":       "0",
    # v10: auth-variabelen expliciet leeg (tests zetten ze zelf via monkeypatch)
    "APP_USER":           "",
    "APP_PASSWORD":       "",
    "API_TOKEN":          "",
    "MASTER_KEY":         "",
})

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:   # 'with' triggert de lifespan (scheduler etc.)
        yield c
