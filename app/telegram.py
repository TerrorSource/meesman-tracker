"""Telegram-notificaties: verzending en berichtopbouw."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import requests

from .core import LOCAL_TZ, decrypt_if_present, fmt_eur, fmt_eur_delta, fmt_pct
from .store import account_labels, deposits_sum_between, period_begin_values, snapshot_values_at

_MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
            "augustus", "september", "oktober", "november", "december"]


def telegram_enabled(cfg: dict) -> bool:
    return bool(
        (cfg.get("telegram_bot_token_enc") or "").strip()
        and (cfg.get("telegram_chat_id_enc") or "").strip()
    )


def _credentials(cfg: dict) -> tuple[str, str]:
    token   = decrypt_if_present(cfg.get("telegram_bot_token_enc")).strip()
    chat_id = decrypt_if_present(cfg.get("telegram_chat_id_enc")).strip()
    return token, chat_id


def send_telegram(cfg: dict, message: str) -> tuple[bool, str]:
    """Send a plain-text Telegram message. Never raises."""
    try:
        if not telegram_enabled(cfg):
            return False, "Telegram not configured"
        token, chat_id = _credentials(cfg)
        if not token or not chat_id:
            return False, "Token/chat_id missing"

        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
        if 200 <= r.status_code < 300:
            return True, "Sent"
        return False, f"HTTP {r.status_code}: {(r.text or '')[:300]}"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_telegram_photo(cfg: dict, photo: Path, caption: str) -> tuple[bool, str]:
    """Stuur een foto (bijv. debug-screenshot) met bijschrift. Never raises."""
    try:
        if not telegram_enabled(cfg):
            return False, "Telegram not configured"
        token, chat_id = _credentials(cfg)
        if not token or not chat_id:
            return False, "Token/chat_id missing"
        with open(photo, "rb") as fh:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": (photo.name, fh, "image/png")},
                timeout=60,
            )
        if 200 <= r.status_code < 300:
            return True, "Sent"
        return False, f"HTTP {r.status_code}: {(r.text or '')[:300]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Saldo-update
# ---------------------------------------------------------------------------
def build_balance_change_message(
    accounts: list,                 # list of AccountRow
    prev_values: dict,              # {account_number: float}
    *,
    min_eur: float = 0.0,           # meld alleen als |Δ totaal| >= min_eur …
    min_pct: float = 0.0,           # … én |Δ% totaal| >= min_pct (0 = altijd)
    exclude: set[str] | None = None,  # gearchiveerde rekeningen
) -> str | None:
    """
    Build a Telegram message if any account balance changed.
    Returns None if nothing changed or the change is below the thresholds.
    """
    exclude    = exclude or set()
    lines      = []
    total      = 0.0
    total_prev = 0.0
    any_change = False
    new_account = False

    date_str = datetime.now(LOCAL_TZ).strftime("%d-%m-%Y")

    for a in sorted(accounts, key=lambda x: x.account_number):
        # Skip blank/phantom rows (e.g. an empty totals row scraped as "  : € 0,00")
        if not (a.label or "").strip() or a.account_number in exclude:
            continue
        total += a.value_eur
        prev = prev_values.get(a.account_number)
        total_prev += prev if prev is not None else a.value_eur

        if prev is None:
            lines.append(f"🆕 {a.label} ({a.account_number})\n   Nu: {fmt_eur(a.value_eur)}")
            any_change = new_account = True
        elif abs(a.value_eur - prev) < 0.005:
            lines.append(f"➡️  {a.label}: {fmt_eur(a.value_eur)} (ongewijzigd)")
        else:
            delta = a.value_eur - prev
            pct   = (delta / prev * 100) if prev else 0.0
            arrow = "📈" if delta >= 0 else "📉"
            lines.append(
                f"{arrow} {a.label} ({a.account_number})\n"
                f"   Was: {fmt_eur(prev)}\n"
                f"   Nu:  {fmt_eur(a.value_eur)} ({fmt_pct(pct)})\n"
                f"   Δ:   {fmt_eur_delta(delta)}"
            )
            any_change = True

    if not any_change:
        return None

    total_delta = total - total_prev
    total_pct   = (total_delta / total_prev * 100) if total_prev else 0.0

    # Drempels (een nieuwe rekening wordt altijd gemeld)
    if not new_account:
        if min_eur > 0 and abs(total_delta) < min_eur:
            return None
        if min_pct > 0 and abs(total_pct) < min_pct:
            return None

    header = f"📊 Meesman saldo update — {date_str}"
    footer = f"\n💰 Totaal: {fmt_eur(total)}"
    if abs(total_delta) >= 0.01:
        footer += f" ({fmt_eur_delta(total_delta)}, {fmt_pct(total_pct)})"

    return header + "\n\n" + "\n\n".join(lines) + "\n" + footer


# ---------------------------------------------------------------------------
# Periode-overzichten (week / maand)
# ---------------------------------------------------------------------------
def build_period_summary(start: datetime, end: datetime, title: str,
                         exclude: set[str] | None = None) -> str | None:
    """
    Overzicht over [start, end): begin- en eindwaarde, inleg, rendement
    (mutatie minus inleg) per rekening en totaal, plus het rendement sinds
    1 januari. None als er geen data is.
    """
    exclude    = exclude or set()
    end_vals   = {a: v for a, v in snapshot_values_at(end).items() if a not in exclude}
    if not end_vals:
        return None
    labels     = account_labels()
    begin_vals = period_begin_values(start)

    total_end   = sum(end_vals.values())
    total_begin = sum(begin_vals.get(a, v) for a, v in end_vals.items())
    dep         = deposits_sum_between(start, end)
    rend        = (total_end - total_begin) - dep
    pct         = (rend / total_begin * 100) if total_begin else 0.0

    lines = [title, "",
             f"💰 Totaal:       {fmt_eur(total_end)}",
             f"   Begin:        {fmt_eur(total_begin)}"]
    if abs(dep) >= 0.01:
        lines.append(f"   Inleg:        {fmt_eur_delta(dep)}")
    lines.append(f"   Rendement:    {fmt_eur_delta(rend)} ({fmt_pct(pct)})")
    lines.append("")

    for acc in sorted(end_vals):
        v_end   = end_vals[acc]
        v_begin = begin_vals.get(acc, v_end)
        delta   = v_end - v_begin
        p       = (delta / v_begin * 100) if v_begin else 0.0
        arrow   = "📈" if delta >= 0 else "📉"
        lines.append(f"{arrow} {labels.get(acc, acc)}: {fmt_eur(v_end)} ({fmt_eur_delta(delta)}, {fmt_pct(p)})")

    # Sinds 1 januari (van het jaar waarin de periode eindigt)
    start_year = end.astimezone(LOCAL_TZ).replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_year < start:
        y_begin = period_begin_values(start_year)
        total_y_begin = sum(y_begin.get(a, v) for a, v in end_vals.items())
        dep_y  = deposits_sum_between(start_year, end)
        rend_y = (total_end - total_y_begin) - dep_y
        pct_y  = (rend_y / total_y_begin * 100) if total_y_begin else 0.0
        lines.append("")
        lines.append(f"📆 Sinds 1 januari: {fmt_eur_delta(rend_y)} ({fmt_pct(pct_y)}) rendement na inleg")

    return "\n".join(lines)


def build_monthly_summary(now: datetime | None = None, exclude: set[str] | None = None) -> str | None:
    """Afgelopen kalendermaand."""
    now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    start_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    py, pm = (start_this.year - 1, 12) if start_this.month == 1 else (start_this.year, start_this.month - 1)
    start_prev = start_this.replace(year=py, month=pm)
    return build_period_summary(start_prev, start_this,
                                f"📅 Meesman maandoverzicht — {_MAANDEN[pm - 1]} {py}", exclude)


def build_weekly_summary(now: datetime | None = None, exclude: set[str] | None = None) -> str | None:
    """Afgelopen 7 dagen (t/m gisteren)."""
    now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    end   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    return build_period_summary(
        start, end,
        f"📅 Meesman weekoverzicht — {start.strftime('%d-%m')} t/m {(end - timedelta(days=1)).strftime('%d-%m-%Y')}",
        exclude,
    )
