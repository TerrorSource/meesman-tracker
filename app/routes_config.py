"""Routes: configuratiepagina en opslaan van instellingen."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from .config_store import load_config, save_config
from .core import cfg_has_key, decrypt_if_present, engine, logger, templates
from .scheduler import scheduler
from .security import encrypt_str, get_or_create_master_key
from .telegram import send_telegram

router = APIRouter()


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    cfg = load_config()

    view = {
        "has_key":          cfg_has_key(cfg),
        "username":         cfg.get("username") or "",
        "refresh_hours":    cfg.get("refresh_hours") or 24,
        "keepalive_minutes": cfg.get("keepalive_minutes") or 30,
        "password_set":     bool((cfg.get("password_enc") or "").strip()),
        "mfa_mode":         cfg.get("mfa_mode") or "manual",

        # TOTP
        "totp_secret_set":  bool((cfg.get("totp_secret_enc") or "").strip()),
        "totp_secret":      decrypt_if_present(cfg.get("totp_secret_enc")).strip(),

        # Manual MFA
        "manual_mfa_set":   bool((cfg.get("manual_mfa_code_enc") or "").strip()),
        "manual_mfa_code":  decrypt_if_present(cfg.get("manual_mfa_code_enc")).strip(),

        # Telegram
        "telegram_bot_set":  bool((cfg.get("telegram_bot_token_enc") or "").strip()),
        "telegram_chat_set": bool((cfg.get("telegram_chat_id_enc") or "").strip()),
        "telegram_bot_token": decrypt_if_present(cfg.get("telegram_bot_token_enc")).strip(),
        "telegram_chat_id":   decrypt_if_present(cfg.get("telegram_chat_id_enc")).strip(),
    }
    return templates.TemplateResponse("config.html", {"request": request, "cfg": view})


@router.post("/config/generate-key")
def generate_key():
    get_or_create_master_key(create=True)
    logger.info("Master key aangemaakt.")
    return RedirectResponse(url="/config?saved=1", status_code=303)


@router.post("/config/save")
def config_save(
    username:          str = Form(""),
    password:          str = Form(""),
    refresh_hours:     int = Form(24),
    keepalive_minutes: int = Form(30),
    mfa_mode:          str = Form("manual"),
    totp_secret:       str = Form(""),
    manual_mfa_code:   str = Form(""),
    telegram_bot_token: str = Form(""),
    telegram_chat_id:   str = Form(""),
):
    cfg = load_config()

    if not cfg_has_key(cfg):
        return RedirectResponse(url="/config?error=no_key", status_code=303)

    # TOTP-geheim valideren vóór opslaan: een typefout merk je anders
    # pas bij de volgende (falende) refresh
    totp_clean = (totp_secret or "").strip().replace(" ", "").upper()
    if totp_clean:
        try:
            import pyotp
            pyotp.TOTP(totp_clean).now()
        except Exception:
            return RedirectResponse(url="/config?error=bad_totp", status_code=303)

    cfg["username"]          = username.strip()
    cfg["refresh_hours"]     = max(1, int(refresh_hours))
    cfg["keepalive_minutes"] = max(5, int(keepalive_minutes))
    cfg["mfa_mode"]          = mfa_mode.strip() or "manual"

    pw = (password or "").strip()
    if pw and pw != "********":
        cfg["password_enc"] = encrypt_str(pw)

    # Velden staan vooringevuld met de huidige waarde in de UI;
    # leeg insturen betekent dus bewust wissen.
    cfg["totp_secret_enc"]        = encrypt_str(totp_clean)                  if totp_clean                 else ""
    cfg["manual_mfa_code_enc"]    = encrypt_str(manual_mfa_code.strip())    if manual_mfa_code.strip()    else ""
    cfg["telegram_bot_token_enc"] = encrypt_str(telegram_bot_token.strip()) if telegram_bot_token.strip() else ""
    cfg["telegram_chat_id_enc"]   = encrypt_str(telegram_chat_id.strip())   if telegram_chat_id.strip()   else ""

    save_config(cfg)

    # Clear keepalive history after config change
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM keepalive_log"))
    except Exception as e:
        logger.warning("Keepalive-log leegmaken mislukt: %s", e)

    scheduler.reschedule_job("refresh_job",   trigger="interval", hours=max(1, int(refresh_hours)))
    scheduler.reschedule_job("keepalive_job", trigger="interval", minutes=max(5, int(keepalive_minutes)))
    logger.info("Config opgeslagen. refresh=%dh keepalive=%dmin mfa_mode=%s",
                max(1, int(refresh_hours)), max(5, int(keepalive_minutes)), mfa_mode)

    return RedirectResponse(url="/config?saved=1", status_code=303)


@router.post("/config/test-telegram")
async def config_test_telegram():
    cfg = load_config()
    ok, _ = await asyncio.to_thread(send_telegram, cfg, "✅ meesman-tracker Telegram test")
    return RedirectResponse(url=f"/config?tg_test={'1' if ok else '0'}&tg_err={'0' if ok else '1'}", status_code=303)
