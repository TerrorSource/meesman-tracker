#!/usr/bin/env python3
"""
Selector-canary: open de Meesman-loginpagina en controleer of de
standaard-selectors uit app/config_store.py nog bestaan.

Exit 0 = alles gevonden, exit 1 = site gewijzigd (of onbereikbaar).
Wordt dagelijks door GitHub Actions gedraaid (zie .github/workflows).
Er wordt NIET ingelogd — alleen de publieke loginpagina wordt bekeken.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config_store import DEFAULT_CONFIG  # noqa: E402

from playwright.async_api import async_playwright  # noqa: E402

LOGIN_URL = "https://login.meesman.nl/"
CHECKS = ["login_user_selector", "login_pass_selector", "login_submit_selector"]


async def main() -> int:
    sels = DEFAULT_CONFIG["selectors"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector(sels["login_user_selector"], timeout=45_000)
        except Exception:
            print(f"❌ Loginformulier niet gevonden ({sels['login_user_selector']}) — "
                  f"eind-URL: {page.url}")
            await browser.close()
            return 1

        missing = [k for k in CHECKS if await page.query_selector(sels[k]) is None]
        await browser.close()

    if missing:
        for k in missing:
            print(f"❌ Ontbreekt: {k} = {sels[k]!r}")
        return 1

    print("✅ Alle login-selectors gevonden op", LOGIN_URL)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
