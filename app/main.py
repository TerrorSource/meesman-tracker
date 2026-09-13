"""App-bootstrap: FastAPI-app, scheduler-jobs, middleware en routers."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config_store import load_config
from .core import APP_VERSION_FULL, LOCAL_TZ, cfg_has_key, logger
from .scheduler import scheduler
from .service_refresh import (
    build_refresh_trigger,
    keepalive_tick,
    monthly_summary_tick,
    refresh_once,
    refresh_stale_threshold_hours,
)
from .store import (
    last_ok_refresh_dt,
    prune_debug_files,
    prune_old_logs,
    restore_deposits_from_json,
    write_export_json,
)
from . import routes_api, routes_config, routes_dashboard, routes_deposits, routes_import


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    keepalive_minutes = max(5, int(cfg.get("keepalive_minutes") or 30))
    refresh_trigger, refresh_desc = build_refresh_trigger(cfg)

    scheduler.add_job(refresh_once, refresh_trigger,
                      id="refresh_job", replace_existing=True,
                      coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(keepalive_tick, "interval", minutes=keepalive_minutes,
                      id="keepalive_job", replace_existing=True,
                      coalesce=True, misfire_grace_time=600)
    # Maandoverzicht: 1e van de maand om 08:00 lokale tijd
    scheduler.add_job(monthly_summary_tick,
                      CronTrigger(day=1, hour=8, minute=0, timezone=LOCAL_TZ),
                      id="monthly_summary", replace_existing=True,
                      coalesce=True, misfire_grace_time=6 * 3600)
    scheduler.start()
    logger.info("Scheduler started (refresh %s, keepalive=%dmin, versie=%s)",
                refresh_desc, keepalive_minutes, APP_VERSION_FULL)
    if cfg.get("mfa_mode") == "totp":
        logger.info("Keepalive is uitgeschakeld in TOTP-modus (de refresh logt zelf opnieuw in).")

    # Opschonen: logtabellen (90 dagen) en debug-dumps met saldi (30 dagen)
    try:
        prune_old_logs(days=90)
        prune_debug_files(days=30)
    except Exception as e:
        logger.warning("Startup: opschonen mislukt: %s", e)

    # Inhaal-refresh: na een (her)start direct verversen als de laatste
    # geslaagde refresh te oud is — anders mis je dagen bij elke deploy
    try:
        last_ok = last_ok_refresh_dt()
        threshold = timedelta(hours=refresh_stale_threshold_hours(cfg))
        stale = last_ok is None or (datetime.now(timezone.utc) - last_ok) > threshold
        if cfg_has_key(cfg) and stale:
            scheduler.add_job(
                refresh_once, "date",
                run_date=datetime.now(timezone.utc) + timedelta(minutes=2),
                id="catchup_refresh", replace_existing=True,
            )
            logger.info("Catch-up refresh ingepland over 2 min (laatste ok: %s)", last_ok)
    except Exception as e:
        logger.warning("Catch-up check mislukt: %s", e)

    try:
        write_export_json()
    except Exception as e:
        logger.warning("Startup: export.json schrijven mislukt: %s", e)

    # Restore deposits from JSON backup if table is empty
    try:
        n = restore_deposits_from_json()
        if n:
            logger.info("Startup: %d inleggen hersteld uit deposits.json", n)
    except Exception as e:
        logger.warning("Startup: deposits-restore mislukt: %s", e)

    yield

    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def same_origin_post_guard(request: Request, call_next):
    """Weiger cross-origin browser-POSTs (CSRF). Clients zonder Origin/Referer
    (curl, Home Assistant) blijven gewoon werken."""
    if request.method == "POST":
        source = request.headers.get("origin") or request.headers.get("referer") or ""
        if source:
            src_host = urlparse(source).netloc
            req_host = request.headers.get("host") or ""
            if src_host and req_host and src_host != req_host:
                return JSONResponse({"detail": "Cross-origin POST geweigerd"}, status_code=403)
    return await call_next(request)


app.include_router(routes_dashboard.router)
app.include_router(routes_api.router)
app.include_router(routes_config.router)
app.include_router(routes_deposits.router)
app.include_router(routes_import.router)
