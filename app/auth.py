"""
Toegangsbeveiliging.

- UI (alle pagina's en formulieren): HTTP Basic Auth met APP_USER / APP_PASSWORD.
- Machine-endpoints (/api/*, /export.json, /deposits.json): API_TOKEN als
  `Authorization: Bearer <token>` of `X-API-Token: <token>`; Basic Auth mag ook.
- /health en /static/ blijven open (Docker-healthcheck, CSS/JS).

Zonder omgevingsvariabelen is de app open, zoals vóór v10 — de update breekt
dus niets totdat je de variabelen zet. De config-pagina waarschuwt dan wel.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response

TOKEN_PREFIXES = ("/api/",)
TOKEN_PATHS    = ("/export.json", "/deposits.json")
OPEN_PATHS     = ("/health",)
OPEN_PREFIXES  = ("/static/",)


class AuthConfig:
    def __init__(self) -> None:
        self.user       = os.environ.get("APP_USER", "").strip()
        self.password   = os.environ.get("APP_PASSWORD", "")
        self.api_token  = os.environ.get("API_TOKEN", "").strip()
        self.fail_delay = 1.0   # vertraging bij foute inlog (brute force remmen)

    @property
    def ui_enabled(self) -> bool:
        return bool(self.user and self.password)

    @property
    def token_enabled(self) -> bool:
        return bool(self.api_token)

    def summary(self) -> dict:
        return {"ui_enabled": self.ui_enabled, "user": self.user, "token_enabled": self.token_enabled}


auth = AuthConfig()


def _basic_ok(request: Request) -> bool:
    hdr = request.headers.get("authorization") or ""
    if not hdr.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(hdr[6:].strip()).decode("utf-8")
    except Exception:
        return False
    user, _, pw = raw.partition(":")
    return secrets.compare_digest(user, auth.user) and secrets.compare_digest(pw, auth.password)


def _token_ok(request: Request) -> bool:
    hdr = request.headers.get("authorization") or ""
    if hdr.lower().startswith("bearer "):
        token = hdr[7:].strip()
    else:
        token = (request.headers.get("x-api-token") or "").strip()
    return bool(token) and secrets.compare_digest(token, auth.api_token)


def is_token_path(path: str) -> bool:
    return path in TOKEN_PATHS or any(path.startswith(p) for p in TOKEN_PREFIXES)


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or any(path.startswith(p) for p in OPEN_PREFIXES)


async def _reject(request: Request, as_json: bool) -> Response:
    # Alleen vertragen bij een échte foute poging (er wérd iets meegestuurd)
    if auth.fail_delay and (request.headers.get("authorization") or request.headers.get("x-api-token")):
        await asyncio.sleep(auth.fail_delay)
    headers = {"WWW-Authenticate": 'Basic realm="meesman-tracker", charset="UTF-8"'}
    if as_json:
        return JSONResponse({"detail": "Niet geautoriseerd"}, status_code=401, headers=headers)
    return Response("Inloggen vereist", status_code=401, headers=headers, media_type="text/plain; charset=utf-8")


async def check_request(request: Request) -> Response | None:
    """Returnt een 401-response als de request geweigerd moet worden, anders None."""
    path = request.url.path
    if is_open_path(path):
        return None

    if is_token_path(path):
        if not auth.ui_enabled and not auth.token_enabled:
            return None
        if auth.token_enabled and _token_ok(request):
            return None
        if auth.ui_enabled and _basic_ok(request):
            return None
        return await _reject(request, as_json=True)

    if not auth.ui_enabled:
        return None
    if _basic_ok(request):
        return None
    return await _reject(request, as_json=False)
