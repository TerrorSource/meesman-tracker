from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Dict
import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/data/config.yaml"))

DEFAULT_SELECTORS: Dict[str, str] = {
    "login_user_selector":    "#login-username",
    "login_pass_selector":    "#login-password",
    "login_submit_selector":  '#login-form button[type="submit"]',
    "mfa_input_selector":     "#two-factor-sign-in-inputcode",
    "mfa_submit_selector":    '#two-factor-sign-in-form button[type="submit"]',
    "accounts_row_selector":  "table.meesman-table tbody tr.grid-row",
    "acc_number_selector":    "td:nth-child(1)",
    "acc_label_selector":     "td:nth-child(3)",
    "acc_value_selector":     "td:nth-child(4) a.text-body",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "master_key": "",
    "username": "",
    "password_enc": "",

    # Planning: vaste dagelijkse refresh-tijd (lokale TZ, "HH:MM"); leeg =
    # terugvallen op het interval in refresh_hours.
    "refresh_time": "07:30",
    "refresh_days": "daily",       # daily | mon-sat | mon-fri
    "refresh_hours": 24,
    "keepalive_minutes": 30,

    # MFA mode: "totp" (automatic, recommended) | "manual" | "none"
    "mfa_mode": "manual",
    "totp_secret_enc": "",       # base32 TOTP secret (encrypted)
    "manual_mfa_code_enc": "",   # single-use code (encrypted)

    # Telegram notifications
    "telegram_bot_token_enc": "",
    "telegram_chat_id_enc": "",

    # Meldingen: alleen een saldo-bericht als de totale wijziging minstens
    # notify_min_eur én notify_min_pct is (0 = altijd melden)
    "notify_min_eur": 0.0,
    "notify_min_pct": 0.0,
    "fail_alert_threshold": 3,   # Telegram-alert na N opeenvolgende mislukte refreshes
    "monthly_summary": True,     # 1e van de maand 08:00
    "weekly_summary": False,     # maandag 08:00

    # Backups: dagelijkse databasekopie in data/backups, zoveel bewaren
    "backup_keep": 14,

    # CSS selectors for scraping (defaults based on current Meesman DOM)
    "selectors": dict(DEFAULT_SELECTORS),
}


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in data.items() if k in merged})

    # Deep-merge selectors (lege waarden → standaard)
    sels = dict(DEFAULT_SELECTORS)
    for k, v in (data.get("selectors") or {}).items():
        if k in sels and (v or "").strip():
            sels[k] = v.strip()
    merged["selectors"] = sels

    return merged


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = dict(DEFAULT_CONFIG)
    out.update({k: cfg.get(k, out[k]) for k in out if k != "selectors"})
    out["selectors"] = dict(DEFAULT_SELECTORS)
    for k, v in (cfg.get("selectors") or {}).items():
        if k in out["selectors"] and (v or "").strip():
            out["selectors"][k] = v.strip()

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
