"""
Toegangsbeveiliging.

- UI (alle pagina's en formulieren): inloggen via /login (formulier, werkt met
  wachtwoordmanagers) → ondertekende sessie-cookie (HttpOnly, SESSION_DAYS).
  HTTP Basic Auth met dezelfde APP_USER / APP_PASSWORD blijft ook werken
  (scripts, curl).
- Machine-endpoints (/api/*, /export.json, /deposits.json): API_TOKEN als
  `Authorization: Bearer <token>` of `X-API-Token: <token>`; Basic Auth of een
  geldige sessie mag ook.
- /health, /static/ en /login blijven open.

Zonder omgevingsvariabelen is de app open, zoals vóór v10 — de update breekt
dus niets totdat je de variabelen zet. De config-pagina waarschuwt dan wel.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

TOKEN_PREFIXES = ("/api/",)
TOKEN_PATHS    = ("/export.json", "/deposits.json")
OPEN_PATHS     = ("/health", "/login")
OPEN_PREFIXES  = ("/static/",)

SESSION_COOKIE = "meesman_session"
SESSION_DAYS   = max(1, int(os.environ.get("SESSION_DAYS", "30") or 30))


class AuthConfig:
    def __init__(self) -> None:
        self.user       = os.environ.get("APP_USER", "").strip()
        self.password   = os.environ.get("APP_PASSWORD", "")
        self.api_token  = os.environ.get("API_TOKEN", "").strip()
        self.fail_delay = 1.0   # vertraging bij foute inlog (brute force remmen)
        self._secret_cache: tuple[tuple, bytes] | None = None

    @property
    def ui_enabled(self) -> bool:
        return bool(self.user and self.password)

    @property
    def token_enabled(self) -> bool:
        return bool(self.api_token)

    def summary(self) -> dict:
        return {"ui_enabled": self.ui_enabled, "user": self.user, "token_enabled": self.token_enabled}

    def credentials_ok(self, user: str, password: str) -> bool:
        return (secrets.compare_digest(user or "", self.user)
                and secrets.compare_digest(password or "", self.password))

    # ── Sessie-cookie (HMAC-ondertekend; sleutel afgeleid van master key + login) ──
    def _secret(self) -> bytes:
        from .security import env_master_key
        from .config_store import load_config
        mk = env_master_key() or (load_config().get("master_key") or "")
        key = (mk, self.user, self.password)
        if self._secret_cache and self._secret_cache[0] == key:
            return self._secret_cache[1]
        secret = hashlib.sha256(f"meesman-session|{mk}|{self.user}|{self.password}".encode()).digest()
        self._secret_cache = (key, secret)
        return secret

    def make_session_token(self) -> str:
        exp = int(time.time()) + SESSION_DAYS * 86400
        sig = hmac.new(self._secret(), f"{self.user}:{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def session_valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        exp_s, sig = token.split(".", 1)
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < time.time():
            return False
        expected = hmac.new(self._secret(), f"{self.user}:{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


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
    return auth.credentials_ok(user, pw)


def _session_ok(request: Request) -> bool:
    return auth.session_valid(request.cookies.get(SESSION_COOKIE))


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


def safe_next(target: str | None) -> str:
    """Alleen interne paden als 'terug naar'-doel (geen open redirect)."""
    t = (target or "").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or t.startswith("/login"):
        return "/"
    return t


def cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https" or (request.headers.get("x-forwarded-proto") or "").lower() == "https"


async def _reject_api(request: Request) -> Response:
    if auth.fail_delay and (request.headers.get("authorization") or request.headers.get("x-api-token")):
        await asyncio.sleep(auth.fail_delay)
    return JSONResponse({"detail": "Niet geautoriseerd"}, status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="meesman-tracker", charset="UTF-8"'})


async def check_request(request: Request) -> Response | None:
    """Returnt een 401/redirect-response als de request geweigerd moet worden, anders None.
    Zet request.state.auth_user voor de templates (uitlogknop)."""
    request.state.auth_user = ""
    path = request.url.path

    if is_open_path(path):
        if auth.ui_enabled and _session_ok(request):
            request.state.auth_user = auth.user
        return None

    if is_token_path(path):
        if not auth.ui_enabled and not auth.token_enabled:
            return None
        if auth.token_enabled and _token_ok(request):
            return None
        if auth.ui_enabled and (_session_ok(request) or _basic_ok(request)):
            request.state.auth_user = auth.user
            return None
        return await _reject_api(request)

    if not auth.ui_enabled:
        return None
    if _session_ok(request) or _basic_ok(request):
        request.state.auth_user = auth.user
        return None

    # Niet ingelogd: browser (GET) naar het inlogscherm, de rest een 401
    if request.method in ("GET", "HEAD"):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=303)
    if auth.fail_delay and request.headers.get("authorization"):
        await asyncio.sleep(auth.fail_delay)
    return Response("Inloggen vereist", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="meesman-tracker", charset="UTF-8"'},
                    media_type="text/plain; charset=utf-8")
