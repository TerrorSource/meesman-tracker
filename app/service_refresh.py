"""Orkestratie van refresh, keepalive, overzichten en backups (scheduler-jobs)."""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from .config_store import load_config
from .core import (
    COOKIES_DUMP_PATH,
    LOCAL_TZ,
    SESSION_STATE_PATH,
    cfg_has_key,
    decrypt_if_present,
    engine,
    logger,
    now_iso,
)
from .scheduler import scheduler
from .scraper import (
    fetch_accounts,
    http_session_check,
    is_browser_launch_failure,
    keepalive_session,
)
from .store import (
    account_labels,
    archived_accounts,
    backup_database,
    consecutive_failed_refreshes,
    get_prev_values,
    latest_debug_screenshot,
    update_missing_accounts,
    write_export_json,
    write_keepalive_log,
    write_refresh_log,
)
from .telegram import (
    build_balance_change_message,
    build_monthly_summary,
    build_weekly_summary,
    send_telegram,
    send_telegram_photo,
    telegram_enabled,
)

# Eén scrape tegelijk (refresh, keepalive of handmatige refresh)
scrape_lock = asyncio.Lock()

# Herkansingen na een mislukte scrape (minuten na de mislukking). Pas als
# ook de laatste herkansing faalt telt de faalreeks richting de alert.
RETRY_DELAYS_MIN = (30, 90)

# Elke N-de keepalive-tick gebruikt de echte browser (ververst sessie/cookies);
# tussendoor volstaat een lichte HTTP-check
BROWSER_KEEPALIVE_EVERY = 8
_keepalive_tick_count = 0
_keepalive_skip_logged = False

# Bij een Chromium-launch-fout (omgevingsprobleem) de container laten
# herstarten via de restart-policy. Uit te zetten met SELF_RESTART=0.
SELF_RESTART_ON_LAUNCH_FAILURE = os.environ.get("SELF_RESTART", "1") != "0"


# ---------------------------------------------------------------------------
# Scheduler-trigger voor de refresh
# ---------------------------------------------------------------------------
_DAYS = {"daily": None, "mon-sat": "mon-sat", "mon-fri": "mon-fri"}


def build_refresh_trigger(cfg: dict):
    """Cron op een vaste lokale kloktijd (refresh_time 'HH:MM', optioneel
    beperkt tot bepaalde dagen), anders terugvallen op het interval in
    refresh_hours. Returnt (trigger, omschrijving)."""
    t = (cfg.get("refresh_time") or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m and 0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60:
        hh, mm = int(m.group(1)), int(m.group(2))
        days = _DAYS.get(cfg.get("refresh_days") or "daily")
        kwargs = {"hour": hh, "minute": mm, "timezone": LOCAL_TZ}
        if days:
            kwargs["day_of_week"] = days
        label = {"mon-sat": "ma–za", "mon-fri": "ma–vr"}.get(days or "", "dagelijks")
        return CronTrigger(**kwargs), f"{label} om {hh:02d}:{mm:02d}"
    hours = max(1, int(cfg.get("refresh_hours") or 24))
    return IntervalTrigger(hours=hours), f"elke {hours} uur"


def refresh_stale_threshold_hours(cfg: dict) -> float:
    """Na hoeveel uur zonder geslaagde refresh een catch-up nodig is."""
    if (cfg.get("refresh_time") or "").strip():
        days = cfg.get("refresh_days") or "daily"
        return {"mon-fri": 26.0 + 48.0, "mon-sat": 26.0 + 24.0}.get(days, 26.0)
    return float(max(1, int(cfg.get("refresh_hours") or 24)))


# ---------------------------------------------------------------------------
# Refresh (met herkansingen)
# ---------------------------------------------------------------------------
async def refresh_once(attempt: int = 0) -> bool:
    """Geserialiseerde refresh: hooguit één scrape tegelijk. Bij een
    mislukte scrape worden herkansingen ingepland. Returnt True bij succes."""
    async with scrape_lock:
        ok, kind = await _do_refresh()

    if not ok and kind == "scrape" and attempt < len(RETRY_DELAYS_MIN):
        delay = RETRY_DELAYS_MIN[attempt]
        try:
            scheduler.add_job(
                refresh_once, "date",
                run_date=datetime.now(timezone.utc) + timedelta(minutes=delay),
                id="retry_refresh", replace_existing=True,
                kwargs={"attempt": attempt + 1},
            )
            logger.warning("Refresh mislukt — herkansing %d/%d over %d minuten.",
                           attempt + 1, len(RETRY_DELAYS_MIN), delay)
        except Exception as e:
            logger.warning("Herkansing inplannen mislukt: %s", e)
    return ok


async def _alert_if_failing(cfg: dict) -> None:
    """Stuur één Telegram-waarschuwing (met screenshot) zodra de faalreeks de
    ingestelde drempel bereikt."""
    if not telegram_enabled(cfg):
        return
    threshold = max(1, int(cfg.get("fail_alert_threshold") or 3))
    streak = consecutive_failed_refreshes()
    if streak != threshold:
        return

    text_msg = (
        f"⚠️ Meesman-tracker: de laatste {streak} refreshes zijn mislukt.\n\n"
        "Controleer de statuspagina (refresh-geschiedenis). Mogelijke oorzaken: "
        "gewijzigde Meesman-site (selectors), verlopen wachtwoord of netwerkproblemen."
    )
    shot = latest_debug_screenshot()
    if shot:
        ok, info = await asyncio.to_thread(send_telegram_photo, cfg, shot,
                                           text_msg + f"\n\n📷 Laatste stap: {shot.stem}")
        if ok:
            logger.info("Telegram faal-alert (met screenshot) verzonden")
            return
        logger.warning("Screenshot versturen mislukt (%s), tekstbericht als fallback", info)
    ok, info = await asyncio.to_thread(send_telegram, cfg, text_msg)
    logger.info("Telegram faal-alert verzonden: ok=%s info=%s", ok, info)


async def _handle_launch_failure(cfg: dict, context: str) -> None:
    """Chromium kon niet starten: direct alerten (dit lost zichzelf niet op)
    en — tenzij uitgeschakeld — het proces beëindigen zodat Docker de
    container schoon herstart (verse PID-namespace, geen zombies)."""
    logger.critical("%s: Chromium kon niet starten — omgevingsprobleem op de host.", context)
    if telegram_enabled(cfg):
        await asyncio.to_thread(
            send_telegram, cfg,
            "🚨 Meesman-tracker: de browser (Chromium) kan niet meer starten.\n\n"
            + ("De container wordt nu automatisch herstart. Blijft dit terugkomen, "
               "controleer dan geheugen en schijfruimte op de NAS."
               if SELF_RESTART_ON_LAUNCH_FAILURE else
               "Herstart de container handmatig (docker restart meesman-tracker)."),
        )
    if SELF_RESTART_ON_LAUNCH_FAILURE:
        logger.critical("Zelf-herstart over 5 seconden (SELF_RESTART=0 om uit te zetten).")
        asyncio.get_running_loop().call_later(5, os._exit, 3)


async def _do_refresh() -> tuple[bool, str]:
    """Returnt (ok, soort): soort is 'ok', 'config' (instellingen ontbreken),
    'scrape' (login/site/netwerk) of 'launch' (Chromium start niet)."""
    cfg = load_config()

    if not cfg_has_key(cfg):
        logger.info("Refresh: no master key, skipping.")
        write_refresh_log("skipped", 0, "No master key configured yet")
        return False, "config"

    username = (cfg.get("username") or "").strip()
    password = decrypt_if_present(cfg.get("password_enc"))

    if not username or not password:
        logger.info("Refresh: username/password missing, skipping.")
        write_refresh_log("skipped", 0, "Missing username/password")
        return False, "config"

    mfa_mode = cfg.get("mfa_mode", "manual")

    # Validate MFA config before hitting the browser
    if mfa_mode == "manual":
        mfa_code = decrypt_if_present(cfg.get("manual_mfa_code_enc")).strip()
        if not mfa_code:
            msg = "Handmatige MFA-code vereist. Voer een nieuwe in via /config."
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            await _alert_if_failing(cfg)
            return False, "config"
        totp_secret = ""
    elif mfa_mode == "totp":
        totp_secret = decrypt_if_present(cfg.get("totp_secret_enc")).strip()
        mfa_code    = ""
        if not totp_secret:
            msg = "TOTP-geheim niet ingesteld. Stel het in via /config."
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            await _alert_if_failing(cfg)
            return False, "config"
    else:
        mfa_code = totp_secret = ""

    logger.info("Refresh: starting (mfa_mode=%s)", mfa_mode)
    t0 = time.monotonic()

    try:
        sels = cfg.get("selectors") or {}
        scrape_cfg = {
            "username":    username,
            "password":    password,
            "mfa_mode":    mfa_mode,
            "mfa_code":    mfa_code,
            "totp_secret": totp_secret,
            **{k: sels[k] for k in sels},
        }

        accounts = await fetch_accounts(
            scrape_cfg,
            storage_state_path=str(SESSION_STATE_PATH),
            dump_cookies_path=str(COOKIES_DUMP_PATH),
        )
        duration = round(time.monotonic() - t0, 1)

        if not accounts:
            msg = "Scrape leverde 0 rekeningen op (login/MFA/selectors mislukt)"
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg, duration)
            await _alert_if_failing(cfg)
            return False, "scrape"

        # ------------------------------------------------------------------
        # Compare with previous values (only store when changed)
        # ------------------------------------------------------------------
        prev_values = get_prev_values()
        ts = now_iso()
        stored = 0

        with engine.begin() as conn:
            for a in accounts:
                prev = prev_values.get(a.account_number)
                if prev is None or abs(a.value_eur - prev) >= 0.005:
                    conn.execute(
                        text("INSERT INTO accounts_snapshot (ts, account_number, label, value_eur) "
                             "VALUES (:ts, :n, :l, :v)"),
                        {"ts": ts, "n": a.account_number, "l": a.label, "v": a.value_eur},
                    )
                    stored += 1

        # Faalreeks bepalen vóór we 'ok' wegschrijven (voor de herstelmelding)
        prior_streak = consecutive_failed_refreshes()

        write_export_json()
        logger.info("Refresh: %d rekeningen opgehaald, %d opgeslagen op %s (%.1fs)",
                    len(accounts), stored, ts, duration)
        write_refresh_log("ok", stored, None, duration)

        # Verdwenen / teruggekeerde rekeningen (eenmalige melding)
        newly_missing, reappeared = update_missing_accounts({a.account_number for a in accounts})
        if reappeared:
            logger.info("Rekening(en) weer aanwezig in de scrape: %s", ", ".join(reappeared))

        # ------------------------------------------------------------------
        # Telegram notifications
        # ------------------------------------------------------------------
        if telegram_enabled(cfg):
            threshold = max(1, int(cfg.get("fail_alert_threshold") or 3))
            if prior_streak >= threshold:
                await asyncio.to_thread(
                    send_telegram, cfg,
                    f"✅ Meesman-tracker: refresh werkt weer (na {prior_streak} mislukte pogingen).",
                )

            if newly_missing:
                labels = account_labels()
                namen = ", ".join(f"{labels.get(a, a)} ({a})" for a in newly_missing)
                await asyncio.to_thread(
                    send_telegram, cfg,
                    f"ℹ️ Meesman-tracker: rekening niet meer gevonden bij Meesman: {namen}.\n\n"
                    "Is de rekening opgeheven? Archiveer hem dan op het dashboard, zodat hij "
                    "niet meer meetelt in totalen en meldingen.",
                )

            msg = build_balance_change_message(
                accounts, prev_values,
                min_eur=float(cfg.get("notify_min_eur") or 0),
                min_pct=float(cfg.get("notify_min_pct") or 0),
                exclude=archived_accounts(),
            )
            if msg:
                ok, info = await asyncio.to_thread(send_telegram, cfg, msg)
                logger.info("Telegram balance update: ok=%s info=%s", ok, info)

        return True, "ok"

    except Exception as e:
        duration = round(time.monotonic() - t0, 1)
        msg = f"Onverwachte fout: {type(e).__name__}: {e}"
        logger.exception("Refresh: %s", msg)
        write_refresh_log("failed", 0, msg, duration)
        if is_browser_launch_failure(e):
            await _handle_launch_failure(cfg, "Refresh")
            return False, "launch"
        await _alert_if_failing(cfg)
        return False, "scrape"


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------
async def keepalive_tick() -> None:
    """
    Houd de sessie warm. Alleen zinvol bij handmatige MFA: met TOTP logt de
    dagelijkse refresh zelf opnieuw in, en elke extra browserstart is dan
    alleen maar belasting (en risico) op de NAS.

    Meestal volstaat een lichte HTTP-check op de opgeslagen cookies; elke
    N-de tick (en bij twijfel) draait de echte browser, die ook de
    sessie-state ververst.
    """
    global _keepalive_tick_count, _keepalive_skip_logged
    cfg = load_config()
    if not cfg_has_key(cfg):
        return

    if cfg.get("mfa_mode", "manual") == "totp":
        if not _keepalive_skip_logged:
            logger.info("Keepalive: overgeslagen — TOTP-modus, de refresh logt zelf opnieuw in.")
            _keepalive_skip_logged = True
        return
    _keepalive_skip_logged = False

    _keepalive_tick_count += 1
    use_browser = (_keepalive_tick_count % BROWSER_KEEPALIVE_EVERY == 1)

    if not use_browser:
        http_ok = await asyncio.to_thread(http_session_check, str(SESSION_STATE_PATH))
        if http_ok is True:
            logger.info("Keepalive: OK (http-check)")
            write_keepalive_log("ok", "http-check")
            return
        # False of None (onduidelijk) → verifieer met de echte browser

    sels = cfg.get("selectors") or {}
    try:
        async with scrape_lock:
            ok = await keepalive_session(
                {
                    "accounts_row_selector": sels.get("accounts_row_selector", ""),
                    "login_user_selector":   sels.get("login_user_selector", ""),
                },
                storage_state_path=str(SESSION_STATE_PATH),
                dump_cookies_path=str(COOKIES_DUMP_PATH),
            )
    except Exception as e:
        logger.warning("Keepalive: onverwachte fout: %s", e)
        if is_browser_launch_failure(e):
            write_keepalive_log("failed", "Chromium kon niet starten")
            await _handle_launch_failure(cfg, "Keepalive")
            return
        ok = False

    if ok:
        logger.info("Keepalive: OK (browser)")
        write_keepalive_log("ok", "browser")
        return

    # Session expired — try to recover automatically if TOTP is configured
    logger.warning("Keepalive: sessie verlopen.")
    write_keepalive_log("failed", "Sessie verlopen (keepalive)")

    mfa_mode    = cfg.get("mfa_mode", "manual")
    totp_secret = decrypt_if_present(cfg.get("totp_secret_enc")).strip()

    if mfa_mode == "totp" and totp_secret:
        logger.info("Keepalive: TOTP beschikbaar — automatisch opnieuw inloggen.")
        write_refresh_log("session_expired", 0, "Sessie verlopen — automatisch herstel gestart (TOTP)")
        recovered = await refresh_once()
        if recovered:
            write_keepalive_log("recovered", "Sessie automatisch hersteld via TOTP")
        else:
            write_keepalive_log("failed", "Automatisch herstel mislukt — zie refresh-log")
    else:
        # Manual MFA: we can't re-login automatically, notify the user
        write_refresh_log("session_expired", 0, "Sessie verlopen (keepalive) — handmatige actie vereist")
        await asyncio.to_thread(
            send_telegram,
            cfg,
            "⚠️ Meesman-tracker: sessie verlopen.\n\n"
            "Open /config, voer een nieuwe MFA-code in, sla op en klik op 'Refresh now'.",
        )


# ---------------------------------------------------------------------------
# Periodieke overzichten en backups
# ---------------------------------------------------------------------------
async def _send_summary(kind: str, builder) -> None:
    cfg = load_config()
    if not telegram_enabled(cfg) or not cfg.get(f"{kind}_summary", False):
        return
    try:
        msg = builder(exclude=archived_accounts())
    except Exception as e:
        logger.warning("%s-overzicht opbouwen mislukt: %s", kind, e)
        return
    if not msg:
        logger.info("%s-overzicht: nog geen data, overgeslagen.", kind)
        return
    ok, info = await asyncio.to_thread(send_telegram, cfg, msg)
    logger.info("Telegram %s-overzicht: ok=%s info=%s", kind, ok, info)


async def monthly_summary_tick() -> None:
    """1e van de maand: overzicht van de afgelopen maand."""
    await _send_summary("monthly", build_monthly_summary)


async def weekly_summary_tick() -> None:
    """Maandagochtend: overzicht van de afgelopen week."""
    await _send_summary("weekly", build_weekly_summary)


async def backup_tick() -> None:
    """Dagelijkse databasekopie (VACUUM INTO) met retentie."""
    cfg = load_config()
    try:
        await asyncio.to_thread(backup_database, max(1, int(cfg.get("backup_keep") or 14)))
    except Exception as e:
        logger.warning("Database-backup mislukt: %s", e)
