"""Orkestratie van refresh en keepalive (scheduler-jobs en handmatige refresh)."""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from .config_store import load_config
from .core import (
    COOKIES_DUMP_PATH,
    SESSION_STATE_PATH,
    cfg_has_key,
    decrypt_if_present,
    engine,
    logger,
    now_iso,
)
from .scraper import fetch_accounts, http_session_check, keepalive_session
from .store import (
    consecutive_failed_refreshes,
    get_prev_values,
    write_export_json,
    write_keepalive_log,
    write_refresh_log,
)
from .telegram import build_balance_change_message, send_telegram, telegram_enabled

# Eén scrape tegelijk (refresh, keepalive of handmatige refresh)
scrape_lock = asyncio.Lock()

# Na dit aantal opeenvolgende mislukte refreshes gaat er één Telegram-alert uit
FAIL_ALERT_THRESHOLD = 3

# Elke N-de keepalive-tick gebruikt de echte browser (ververst sessie/cookies);
# tussendoor volstaat een lichte HTTP-check
BROWSER_KEEPALIVE_EVERY = 8
_keepalive_tick_count = 0


async def refresh_once() -> bool:
    """Geserialiseerde refresh: hooguit één scrape tegelijk. Returnt True bij succes."""
    async with scrape_lock:
        return await _do_refresh()


async def _alert_if_failing(cfg: dict) -> None:
    """Stuur één Telegram-waarschuwing zodra de faalreeks de drempel bereikt."""
    if not telegram_enabled(cfg):
        return
    streak = consecutive_failed_refreshes()
    if streak == FAIL_ALERT_THRESHOLD:
        ok, info = await asyncio.to_thread(
            send_telegram,
            cfg,
            f"⚠️ Meesman-tracker: de laatste {streak} refreshes zijn mislukt.\n\n"
            "Controleer de refresh-log op het dashboard. Mogelijke oorzaken: "
            "gewijzigde Meesman-site (selectors), verlopen wachtwoord of netwerkproblemen.",
        )
        logger.info("Telegram faal-alert verzonden: ok=%s info=%s", ok, info)


async def _do_refresh() -> bool:
    cfg = load_config()

    if not cfg_has_key(cfg):
        logger.info("Refresh: no master key, skipping.")
        write_refresh_log("skipped", 0, "No master key configured yet")
        return False

    username = (cfg.get("username") or "").strip()
    password = decrypt_if_present(cfg.get("password_enc"))

    if not username or not password:
        logger.info("Refresh: username/password missing, skipping.")
        write_refresh_log("skipped", 0, "Missing username/password")
        return False

    mfa_mode = cfg.get("mfa_mode", "manual")

    # Validate MFA config before hitting the browser
    if mfa_mode == "manual":
        mfa_code = decrypt_if_present(cfg.get("manual_mfa_code_enc")).strip()
        if not mfa_code:
            msg = "Handmatige MFA-code vereist. Voer een nieuwe in via /config."
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            await _alert_if_failing(cfg)
            return False
        totp_secret = ""
    elif mfa_mode == "totp":
        totp_secret = decrypt_if_present(cfg.get("totp_secret_enc")).strip()
        mfa_code    = ""
        if not totp_secret:
            msg = "TOTP-geheim niet ingesteld. Stel het in via /config."
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            await _alert_if_failing(cfg)
            return False
    else:
        mfa_code = totp_secret = ""

    logger.info("Refresh: starting (mfa_mode=%s)", mfa_mode)

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

        if not accounts:
            msg = "Scrape leverde 0 rekeningen op (login/MFA/selectors mislukt)"
            logger.warning("Refresh: %s", msg)
            write_refresh_log("failed", 0, msg)
            await _alert_if_failing(cfg)
            return False

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
        logger.info("Refresh: %d rekeningen opgehaald, %d opgeslagen op %s", len(accounts), stored, ts)
        write_refresh_log("ok", stored, None)

        # ------------------------------------------------------------------
        # Telegram notifications
        # ------------------------------------------------------------------
        if telegram_enabled(cfg):
            if prior_streak >= FAIL_ALERT_THRESHOLD:
                await asyncio.to_thread(
                    send_telegram, cfg,
                    f"✅ Meesman-tracker: refresh werkt weer (na {prior_streak} mislukte pogingen).",
                )

            msg = build_balance_change_message(accounts, prev_values)
            if msg:
                ok, info = await asyncio.to_thread(send_telegram, cfg, msg)
                logger.info("Telegram balance update: ok=%s info=%s", ok, info)

        return True

    except Exception as e:
        msg = f"Onverwachte fout: {type(e).__name__}: {e}"
        logger.exception("Refresh: %s", msg)
        write_refresh_log("failed", 0, msg)
        await _alert_if_failing(cfg)
        return False


async def keepalive_tick() -> None:
    """
    Houd de sessie warm. Meestal volstaat een lichte HTTP-check op de
    opgeslagen cookies; elke N-de tick (en bij twijfel) draait de echte
    browser, die ook de sessie-state ververst.
    Bij een verlopen sessie + TOTP wordt automatisch opnieuw ingelogd.
    """
    global _keepalive_tick_count
    cfg = load_config()
    if not cfg_has_key(cfg):
        return

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
                {"accounts_row_selector": sels.get("accounts_row_selector", "")},
                storage_state_path=str(SESSION_STATE_PATH),
                dump_cookies_path=str(COOKIES_DUMP_PATH),
            )
    except Exception as e:
        logger.warning("Keepalive: onverwachte fout: %s", e)
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
