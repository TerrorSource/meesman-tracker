"""Routes: configuratiepagina en opslaan van instellingen."""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from .config_store import DEFAULT_SELECTORS, load_config, save_config
from .core import cfg_has_key, decrypt_if_present, engine, logger, templates
from .scheduler import scheduler
from .security import encrypt_str, get_or_create_master_key
from .service_refresh import build_refresh_trigger
from .telegram import send_telegram

router = APIRouter()

SELECTOR_KEYS = list(DEFAULT_SELECTORS.keys())


def _num(s: str, default: float = 0.0) -> float:
    """Formulier-getal met komma óf punt als decimaalteken."""
    s = (s or "").strip().replace("€", "").replace("%", "").replace(",", ".")
    try:
        return float(s) if s else default
    except ValueError:
        return default


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    cfg = load_config()

    view = {
        "has_key":          cfg_has_key(cfg),
        "username":         cfg.get("username") or "",
        "refresh_time":     cfg.get("refresh_time") or "",
        "refresh_days":     cfg.get("refresh_days") or "daily",
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

        # Meldingen
        "notify_min_eur":       cfg.get("notify_min_eur") or 0,
        "notify_min_pct":       cfg.get("notify_min_pct") or 0,
        "fail_alert_threshold": cfg.get("fail_alert_threshold") or 3,
        "monthly_summary":      bool(cfg.get("monthly_summary", True)),
        "weekly_summary":       bool(cfg.get("weekly_summary", False)),
        "notify_messages":      bool(cfg.get("notify_messages", True)),

        # Backups
        "backup_keep":      cfg.get("backup_keep") or 14,

        # Selectors
        "selectors":        cfg.get("selectors") or {},
        "selector_defaults": DEFAULT_SELECTORS,
        "selector_keys":    SELECTOR_KEYS,
    }
    return templates.TemplateResponse(request, "config.html", {"cfg": view})


@router.post("/config/generate-key")
def generate_key():
    get_or_create_master_key(create=True)
    logger.info("Master key aangemaakt.")
    return RedirectResponse(url="/config?saved=1", status_code=303)


@router.post("/config/save")
async def config_save(request: Request):
    form = await request.form()
    g = lambda k, d="": (form.get(k) if form.get(k) is not None else d)  # noqa: E731

    cfg = load_config()
    if not cfg_has_key(cfg):
        return RedirectResponse(url="/config?error=no_key", status_code=303)

    # ── Planning ──
    refresh_time = str(g("refresh_time")).strip()
    if refresh_time:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", refresh_time)
        if not (m and 0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60):
            return RedirectResponse(url="/config?error=bad_time", status_code=303)
        refresh_time = f"{int(m.group(1)):02d}:{m.group(2)}"
    refresh_days = str(g("refresh_days", "daily"))
    if refresh_days not in ("daily", "mon-sat", "mon-fri"):
        refresh_days = "daily"

    # ── TOTP-geheim valideren vóór opslaan ──
    totp_clean = str(g("totp_secret")).strip().replace(" ", "").upper()
    if totp_clean:
        try:
            import pyotp
            pyotp.TOTP(totp_clean).now()
        except Exception:
            return RedirectResponse(url="/config?error=bad_totp", status_code=303)

    cfg["username"]          = str(g("username")).strip()
    cfg["refresh_time"]      = refresh_time
    cfg["refresh_days"]      = refresh_days
    cfg["refresh_hours"]     = max(1, int(_num(str(g("refresh_hours", "24")), 24)))
    cfg["keepalive_minutes"] = max(5, int(_num(str(g("keepalive_minutes", "30")), 30)))
    cfg["mfa_mode"]          = str(g("mfa_mode", "manual")).strip() or "manual"

    pw = str(g("password")).strip()
    if pw and pw != "********":
        cfg["password_enc"] = encrypt_str(pw)

    # Velden staan vooringevuld met de huidige waarde in de UI;
    # leeg insturen betekent dus bewust wissen.
    manual_code = str(g("manual_mfa_code")).strip()
    bot_token   = str(g("telegram_bot_token")).strip()
    chat_id     = str(g("telegram_chat_id")).strip()
    cfg["totp_secret_enc"]        = encrypt_str(totp_clean)  if totp_clean  else ""
    cfg["manual_mfa_code_enc"]    = encrypt_str(manual_code) if manual_code else ""
    cfg["telegram_bot_token_enc"] = encrypt_str(bot_token)   if bot_token   else ""
    cfg["telegram_chat_id_enc"]   = encrypt_str(chat_id)     if chat_id     else ""

    # ── Meldingen ──
    cfg["notify_min_eur"]       = max(0.0, _num(str(g("notify_min_eur", "0"))))
    cfg["notify_min_pct"]       = max(0.0, _num(str(g("notify_min_pct", "0"))))
    cfg["fail_alert_threshold"] = max(1, int(_num(str(g("fail_alert_threshold", "3")), 3)))
    cfg["monthly_summary"]      = g("monthly_summary") == "1"
    cfg["weekly_summary"]       = g("weekly_summary") == "1"
    cfg["notify_messages"]      = g("notify_messages") == "1"

    # ── Backups ──
    cfg["backup_keep"] = max(1, int(_num(str(g("backup_keep", "14")), 14)))

    # ── Selectors (leeg = standaard) ──
    sels = {}
    for k in SELECTOR_KEYS:
        v = str(g(f"sel_{k}")).strip()
        sels[k] = v or DEFAULT_SELECTORS[k]
    cfg["selectors"] = sels

    save_config(cfg)

    # Clear keepalive history after config change
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM keepalive_log"))
    except Exception as e:
        logger.warning("Keepalive-log leegmaken mislukt: %s", e)

    trigger, desc = build_refresh_trigger(cfg)
    scheduler.reschedule_job("refresh_job",   trigger=trigger)
    scheduler.reschedule_job("keepalive_job", trigger="interval", minutes=cfg["keepalive_minutes"])
    logger.info("Config opgeslagen. refresh %s, keepalive=%dmin, mfa_mode=%s",
                desc, cfg["keepalive_minutes"], cfg["mfa_mode"])

    return RedirectResponse(url="/config?saved=1", status_code=303)


@router.post("/config/selectors/reset")
def config_selectors_reset():
    cfg = load_config()
    cfg["selectors"] = dict(DEFAULT_SELECTORS)
    save_config(cfg)
    logger.info("Selectors teruggezet naar standaard.")
    return RedirectResponse(url="/config?saved=1", status_code=303)


@router.post("/config/test-telegram")
async def config_test_telegram():
    cfg = load_config()
    ok, _ = await asyncio.to_thread(send_telegram, cfg, "✅ meesman-tracker Telegram test")
    return RedirectResponse(url=f"/config?tg_test={'1' if ok else '0'}&tg_err={'0' if ok else '1'}", status_code=303)
