"""Telegram-notificaties: verzending en berichtopbouw."""
from __future__ import annotations

from datetime import datetime

import requests

from .core import LOCAL_TZ, decrypt_if_present, fmt_eur, fmt_eur_delta, fmt_pct


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
