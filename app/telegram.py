"""Telegram-notificaties: verzending en berichtopbouw."""
from __future__ import annotations

from datetime import datetime

import requests

from .core import LOCAL_TZ, decrypt_if_present, fmt_eur, fmt_eur_delta, fmt_pct
from .store import (
    account_labels,
    deposits_sum_between,
    snapshot_values_at,
    snapshot_values_first_from,
)

_MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
            "augustus", "september", "oktober", "november", "december"]


def telegram_enabled(cfg: dict) -> bool:
    return bool(
        (cfg.get("telegram_bot_token_enc") or "").strip()
        and (cfg.get("telegram_chat_id_enc") or "").strip()
    )


def send_telegram(cfg: dict, message: str) -> tuple[bool, str]:
    """Send a plain-text Telegram message. Never raises."""
    try:
        if not telegram_enabled(cfg):
            return False, "Telegram not configured"

        token   = decrypt_if_present(cfg.get("telegram_bot_token_enc")).strip()
        chat_id = decrypt_if_present(cfg.get("telegram_chat_id_enc")).strip()
        if not token or not chat_id:
            return False, "Token/chat_id missing"

        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
        if 200 <= r.status_code < 300:
            return True, "Sent"

        body = (r.text or "")[:300]
        return False, f"HTTP {r.status_code}: {body}"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def build_balance_change_message(
    accounts: list,        # list of AccountRow
    prev_values: dict,     # {account_number: float}
) -> str | None:
    """
    Build a Telegram message if any account balance changed.
    Returns None if nothing changed.
    """
    lines      = []
    total      = 0.0
    total_prev = 0.0
    any_change = False

    date_str = datetime.now(LOCAL_TZ).strftime("%d-%m-%Y")

    for a in sorted(accounts, key=lambda x: x.account_number):
        # Skip blank/phantom rows (e.g. an empty totals row scraped as "  : € 0,00")
        if not (a.label or "").strip():
            continue
        total += a.value_eur
        prev = prev_values.get(a.account_number)
        total_prev += prev if prev is not None else a.value_eur

        if prev is None:
            lines.append(f"🆕 {a.label} ({a.account_number})\n   Nu: {fmt_eur(a.value_eur)}")
            any_change = True
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

    header = f"📊 Meesman saldo update — {date_str}"
    footer = f"\n💰 Totaal: {fmt_eur(total)}"
    if abs(total_delta) >= 0.01:
        footer += f" ({fmt_eur_delta(total_delta)}, {fmt_pct(total_pct)})"

    return header + "\n\n" + "\n\n".join(lines) + "\n" + footer


def build_monthly_summary(now: datetime | None = None) -> str | None:
    """
    Maandoverzicht over de afgelopen kalendermaand: begin- en eindwaarde,
    inleg in die maand, rendement (mutatie minus inleg) per rekening en
    totaal, plus het rendement sinds 1 januari. None als er geen data is.
    """
    now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)

    # Afgelopen kalendermaand: [start_prev, start_this)
    start_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_year, prev_month = (start_this.year - 1, 12) if start_this.month == 1 else (start_this.year, start_this.month - 1)
    start_prev = start_this.replace(year=prev_year, month=prev_month)
    start_year = start_this.replace(month=1, day=1)

    end_vals   = snapshot_values_at(start_this)
    labels     = account_labels()
    if not end_vals:
        return None

    # Beginwaarde: laatste meting vóór de peildatum; bestaat die niet (rekening
    # of tracker begon later), dan de eerste meting erná binnen de periode.
    def _begin_values(period_start: datetime) -> dict[str, float]:
        vals = snapshot_values_first_from(period_start)
        vals.update(snapshot_values_at(period_start))
        return vals

    begin_vals = _begin_values(start_prev)
    year_vals  = _begin_values(start_year)

    total_end   = sum(end_vals.values())
    total_begin = sum(begin_vals.get(a, v) for a, v in end_vals.items())
    dep_month   = deposits_sum_between(start_prev, start_this)
    rend_month  = (total_end - total_begin) - dep_month
    pct_month   = (rend_month / total_begin * 100) if total_begin else 0.0

    lines = [
        f"📅 Meesman maandoverzicht — {_MAANDEN[prev_month - 1]} {prev_year}",
        "",
        f"💰 Totaal:       {fmt_eur(total_end)}",
        f"   Begin maand:  {fmt_eur(total_begin)}",
    ]
    if abs(dep_month) >= 0.01:
        lines.append(f"   Inleg:        {fmt_eur_delta(dep_month)}")
    lines.append(f"   Rendement:    {fmt_eur_delta(rend_month)} ({fmt_pct(pct_month)})")
    lines.append("")

    for acc in sorted(end_vals):
        v_end   = end_vals[acc]
        v_begin = begin_vals.get(acc, v_end)
        delta   = v_end - v_begin
        pct     = (delta / v_begin * 100) if v_begin else 0.0
        arrow   = "📈" if delta >= 0 else "📉"
        lines.append(f"{arrow} {labels.get(acc, acc)}: {fmt_eur(v_end)} ({fmt_eur_delta(delta)}, {fmt_pct(pct)})")

    if year_vals and start_year < start_this:
        total_year_begin = sum(year_vals.get(a, v) for a, v in end_vals.items())
        dep_year  = deposits_sum_between(start_year, start_this)
        rend_year = (total_end - total_year_begin) - dep_year
        pct_year  = (rend_year / total_year_begin * 100) if total_year_begin else 0.0
        lines.append("")
        lines.append(f"📆 Sinds 1 januari: {fmt_eur_delta(rend_year)} ({fmt_pct(pct_year)}) rendement na inleg")

    return "\n".join(lines)
