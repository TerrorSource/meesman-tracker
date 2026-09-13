"""Routes: inlogpagina (formulier) en uitloggen."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import SESSION_COOKIE, SESSION_DAYS, auth, cookie_secure, safe_next
from .core import logger, templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not auth.ui_enabled or auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url=safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": None})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    if not auth.ui_enabled:
        return RedirectResponse(url="/", status_code=303)

    if not auth.credentials_ok(username.strip(), password):
        if auth.fail_delay:
            await asyncio.sleep(auth.fail_delay)
        logger.warning("Mislukte inlogpoging voor gebruiker %r", username.strip()[:40])
        return templates.TemplateResponse(
            request, "login.html",
            {"next": safe_next(next), "error": "Onjuiste gebruikersnaam of wachtwoord."},
            status_code=401,
        )

    resp = RedirectResponse(url=safe_next(next), status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, auth.make_session_token(),
        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=cookie_secure(request), path="/",
    )
    logger.info("Ingelogd: %s", auth.user)
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
