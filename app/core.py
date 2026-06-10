"""Gedeelde basis: logging, paden, engine, templates en kleine helpers."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from .db import get_engine, init_db
from .scraper import parse_eur_text
from .security import decrypt_str

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("meesman")

# ---------------------------------------------------------------------------
# Versie (door CI als build-arg meegegeven; lokaal 'dev')
# ---------------------------------------------------------------------------
APP_VERSION = os.environ.get("APP_VERSION", "dev")
APP_COMMIT  = (os.environ.get("APP_COMMIT") or "")[:7]
APP_VERSION_FULL = f"{APP_VERSION} ({APP_COMMIT})" if APP_COMMIT else APP_VERSION

# ---------------------------------------------------------------------------
# Lokale tijdzone voor weergave (opslag blijft UTC)
# ---------------------------------------------------------------------------
try:
    LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
except Exception:
    LOCAL_TZ = timezone.utc

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR           = Path(os.environ.get("DATA_DIR", "/data"))
EXPORT_PATH        = Path(os.environ.get("EXPORT_PATH",        str(DATA_DIR / "export.json")))
SESSION_STATE_PATH = Path(os.environ.get("SESSION_STATE_PATH", str(DATA_DIR / "session.json")))
COOKIES_DUMP_PATH  = Path(os.environ.get("COOKIES_DUMP_PATH",  str(DATA_DIR / "cookies.json")))
DEPOSITS_PATH      = Path(os.environ.get("DEPOSITS_PATH",      str(DATA_DIR / "deposits.json")))

# ---------------------------------------------------------------------------
# DB + templates (singletons)
# ---------------------------------------------------------------------------
engine = get_engine()
init_db(engine)

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = APP_VERSION_FULL


# ---------------------------------------------------------------------------
# Kleine helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_timedelta(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def cfg_has_key(cfg: dict) -> bool:
    return bool((cfg.get("master_key") or "").strip())


def decrypt_if_present(enc: str | None) -> str:
    if not enc:
        return ""
    try:
        return decrypt_str(enc)
    except Exception:
        return ""


def to_float(v) -> float:
    """Normaliseer een DB-waarde (REAL, of legacy tekst in NL-notatie) naar float."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return parse_eur_text(str(v))


def parse_form_amount(s: str) -> float:
    """Parse NL-formulierinvoer ('29.869,81' / '4987,50'). Komma is verplicht;
    raises ValueError bij ongeldige invoer."""
    v = (s or "").strip().replace("€", "").strip()
    v = re.sub(r"[^0-9,\-]", "", v)
    if v.count(",") != 1:
        raise ValueError("komma als decimaalteken vereist")
    return float(v.replace(",", "."))


def fmt_eur(v: float) -> str:
    """Format as Dutch currency: 30180.36 → '€ 30.180,36'"""
    formatted = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {formatted}"


def fmt_eur_delta(v: float) -> str:
    """Signed delta with the sign before the symbol: 87.93 → '+€ 87,93', -435.54 → '-€ 435,54'."""
    sign = "+" if v >= 0 else "-"
    return f"{sign}{fmt_eur(abs(v))}"


def fmt_pct(v: float) -> str:
    """Signed Dutch percentage: 0.27 → '+0,27%', -1.3 → '-1,30%'"""
    return f"{v:+.2f}%".replace(".", ",")
