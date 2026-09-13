import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TZ = os.environ.get("TZ", "UTC")

try:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TZ)
    scheduler = AsyncIOScheduler(timezone=tz)
except Exception:
    scheduler = AsyncIOScheduler()
