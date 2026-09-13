from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import requests
from playwright.async_api import async_playwright

_HTTP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

logger = logging.getLogger("meesman")

# Sinds de login-migratie (juli 2026) serveert mijn.meesman.nl zonder geldige
# sessie een 200 met alleen een JS-redirect-stubje naar de OAuth-flow.
_STUB_MARKER = "signInWithMeesmanAuthentication"

# Max. aantal API-responses dat per refresh wordt vastgelegd (api_capture.json)
_API_CAPTURE_LIMIT = 50


def is_browser_launch_failure(exc: BaseException) -> bool:
    """True als Chromium zelf niet kon starten (omgevingsprobleem op de host:
    zombie-processen, geheugen, schijf). Dat lost zichzelf nooit op door
    opnieuw te proberen — daar hoort een directe alert + herstart bij."""
    return "BrowserType.launch" in str(exc)


def _is_redirect_stub(body: str) -> bool:
    """Herkent het ~200-bytes JS-stubje dat mijn.meesman.nl zonder sessie teruggeeft."""
    return len(body) < 2000 and _STUB_MARKER in body


# ---------------------------------------------------------------------------
# TOTP support (pyotp is optional; only needed when mfa_mode == "totp")
# ---------------------------------------------------------------------------
def _generate_totp(secret: str) -> str:
    """Generate current TOTP code from a base32 secret."""
    import pyotp
    return pyotp.TOTP(secret).now()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cookie_expires_iso(expires: float | int | None) -> Optional[str]:
    if expires is None:
        return None
    try:
        exp = float(expires)
    except Exception:
        return None
    if exp <= 0:
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    except Exception:
        return None


async def dump_cookies(context, dump_path: str) -> dict[str, Any]:
    """Dumps Playwright context cookie *metadata* to JSON (no values)."""
    cookies = await context.cookies()
    out = []
    soonest = None
    for c in cookies:
        exp_iso = _cookie_expires_iso(c.get("expires"))
        if exp_iso and (soonest is None or exp_iso < soonest):
            soonest = exp_iso
        # Alleen metadata — de cookie-wáárdes staan al in session.json en
        # hoeven niet nog een keer in platte tekst op schijf.
        out.append({
            "name":        c.get("name"),
            "domain":      c.get("domain"),
            "path":        c.get("path"),
            "expires":     c.get("expires"),
            "expires_iso": exp_iso,
            "secure":      c.get("secure"),
            "httpOnly":    c.get("httpOnly"),
        })

    payload = {
        "generated_at": _now_iso(),
        "cookie_count": len(cookies),
        "soonest_expires_iso": soonest,
        "cookies": out,
    }

    p = Path(dump_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class AccountRow:
    account_number: str
    label: str
    value_eur: float


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def parse_eur_text(s: str) -> float:
    """Parse Dutch-formatted currency strings like '€ 29.869,81' → 29869.81"""
    s = s.strip().replace("€", "").replace(" ", " ")
    s = re.sub(r"[^\d,.\-]", "", s)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        # Alleen punten, gegroepeerd per drie → duizendtallen ('1.234' → 1234)
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


# Oude interne naam blijft geldig voor bestaande aanroepen
_parse_eur = parse_eur_text


def _digits_only(s: str) -> str:
    """Extract digits from strings like '👤 22404586' → '22404586'"""
    m = re.findall(r"\d+", s)
    return "".join(m) if m else s.strip()


async def parse_accounts_from_page(page, cfg: dict) -> tuple[List[AccountRow], bool]:
    """
    Lees de rekeningtabel uit een (al geladen) pagina.

    Eerst via de geconfigureerde selectors (primair); lukt dat niet, dan
    een terugval die elke tabel met 'rekeningnummer' + 'waarde' doorzoekt.
    Returnt (rekeningen, primair_gelukt). Wordt ook offline getest tegen
    een HTML-fixture (tests/fixtures).
    """
    accounts: List[AccountRow] = []

    # ------------------------------------------------------------------
    # Primary selector: known Meesman desktop table
    # ------------------------------------------------------------------
    try:
        await page.wait_for_selector("table.meesman-table", timeout=15_000)
        rows = await page.query_selector_all(cfg["accounts_row_selector"])

        for r in rows:
            num_el = await r.query_selector(cfg["acc_number_selector"])
            lab_el = await r.query_selector(cfg["acc_label_selector"])
            val_el = (
                await r.query_selector(cfg["acc_value_selector"])
                or await r.query_selector("td:nth-child(4)")
            )

            if not (num_el and lab_el and val_el):
                continue

            num = _digits_only(await num_el.inner_text())
            lab = (await lab_el.inner_text()).strip()
            val = _parse_eur((await val_el.inner_text()).strip())

            # Skip blank/phantom rows (e.g. an empty totals row with no number/label)
            if not (num and lab):
                continue

            accounts.append(AccountRow(account_number=num, label=lab, value_eur=val))

        if accounts:
            return accounts, True
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Fallback: find any table with "rekeningnummer" + "waarde"
    # ------------------------------------------------------------------
    for t in await page.query_selector_all("table"):
        try:
            txt = (await t.inner_text()).lower()
        except Exception:
            continue

        if "rekeningnummer" in txt and "waarde" in txt:
            for r in await t.query_selector_all("tbody tr"):
                tds = await r.query_selector_all("td")
                if len(tds) < 4:
                    continue
                num = _digits_only(await tds[0].inner_text())
                lab = (await tds[2].inner_text()).strip()
                val = _parse_eur(await tds[3].inner_text())
                if num and lab:
                    accounts.append(AccountRow(account_number=num, label=lab, value_eur=val))
            break

    return accounts, False


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------
async def fetch_accounts(
    cfg: dict,
    *,
    storage_state_path: str | None = None,
    save_storage_state: bool = True,
    dump_cookies_path: str | None = None,
) -> List[AccountRow]:
    """
    Log in to Meesman (with optional TOTP or manual MFA), scrape account balances.

    cfg keys:
      username, password, mfa_mode ("totp"|"manual"|"none"),
      totp_secret (plain text, decrypted), mfa_code (manual code),
      login_user_selector, login_pass_selector, login_submit_selector,
      mfa_input_selector, mfa_submit_selector,
      accounts_row_selector, acc_number_selector, acc_label_selector, acc_value_selector
    """
    login_url = "https://login.meesman.nl/"
    home_url  = "https://mijn.meesman.nl/"

    debug_dir = Path(os.environ.get("DEBUG_DIR", "/data/debug"))
    debug_dir.mkdir(parents=True, exist_ok=True)

    async def dump(page, name: str) -> None:
        try:
            await page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
        except Exception:
            pass
        try:
            (debug_dir / f"{name}.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # API-capture: leg JSON-responses van *.meesman.nl vast (url, status,
    # preview) in debug/api_capture.json. Doel: onderzoeken of het
    # rekeningoverzicht als JSON beschikbaar is, zodat DOM-scraping op
    # termijn vervangen kan worden door een API-call.
    # ------------------------------------------------------------------
    captured: list[dict] = []

    async def _capture_response(resp) -> None:
        try:
            url = resp.url
            if "meesman.nl" not in url or len(captured) >= _API_CAPTURE_LIMIT:
                return
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            entry: dict = {"url": url, "status": resp.status, "content_type": ctype}
            try:
                body = await resp.text()
                entry["size"] = len(body)
                entry["preview"] = body[:2000]
            except Exception:
                pass
            captured.append(entry)
        except Exception:
            pass

    def _write_capture() -> None:
        try:
            (debug_dir / "api_capture.json").write_text(
                json.dumps({"generated_at": _now_iso(), "responses": captured},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # Resolve MFA code before starting the browser
    mfa_mode = cfg.get("mfa_mode", "manual")
    mfa_code = ""
    if mfa_mode == "totp":
        secret = (cfg.get("totp_secret") or "").strip()
        if secret:
            mfa_code = _generate_totp(secret)
    elif mfa_mode == "manual":
        mfa_code = (cfg.get("mfa_code") or "").strip()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        ctx_kwargs = {}
        if storage_state_path and Path(storage_state_path).exists():
            ctx_kwargs["storage_state"] = storage_state_path

        ctx  = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        page.on("response", _capture_response)

        # ------------------------------------------------------------------
        # 1) Navigate to login – reuse session if possible
        # ------------------------------------------------------------------
        # Wacht op het loginformulier óf een al-ingelogde rekeningtabel
        # (gecombineerde CSS-selector). Ruime timeout: de loginflow loopt via
        # een externe identity-provider en kan op NAS-hardware traag zijn.
        combined_selector = f'{cfg["login_user_selector"]}, {cfg["accounts_row_selector"]}'

        await page.goto(login_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(combined_selector, timeout=45_000)
        except Exception:
            await dump(page, "step0_login_timeout")
            if not ctx_kwargs:
                # Er was al geen opgeslagen sessie — niets om weg te gooien
                raise

            # Zelfherstel: een verouderde opgeslagen sessie kan de (gewijzigde)
            # loginflow blokkeren. Gooi de sessie weg en probeer één keer
            # opnieuw met een schone browsercontext.
            logger.warning(
                "Login-wachtstap faalde met opgeslagen sessie — "
                "sessie wordt gewist, retry met schone context."
            )
            await ctx.close()
            try:
                Path(storage_state_path).unlink(missing_ok=True)
            except Exception:
                pass

            ctx  = await browser.new_context()
            page = await ctx.new_page()
            page.on("response", _capture_response)
            await page.goto(login_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(combined_selector, timeout=45_000)
            except Exception:
                await dump(page, "step0_login_timeout_retry")
                raise

        logged_in = (await page.query_selector(cfg["accounts_row_selector"])) is not None

        if not logged_in:
            await page.fill(cfg["login_user_selector"], cfg["username"])
            await page.fill(cfg["login_pass_selector"], cfg["password"])
            await page.click(cfg["login_submit_selector"])
            await page.wait_for_timeout(1_500)
            await dump(page, "step1_after_login")
        else:
            await dump(page, "step1_session_reused")

        # ------------------------------------------------------------------
        # 2) MFA (if shown)
        # ------------------------------------------------------------------
        try:
            await page.wait_for_selector(cfg["mfa_input_selector"], timeout=12_000)

            if mfa_code:
                await page.fill(cfg["mfa_input_selector"], mfa_code)
                await page.click(cfg["mfa_submit_selector"])
                await page.wait_for_timeout(1_500)
                await dump(page, "step2_after_mfa")
            else:
                # MFA field appeared but we have no code
                await dump(page, "step2_mfa_no_code")
                _write_capture()
                await ctx.close()
                await browser.close()
                return []
        except Exception:
            await dump(page, "step2_mfa_not_found")

        # ------------------------------------------------------------------
        # 3) Navigate to account overview
        # ------------------------------------------------------------------
        await page.goto(home_url, wait_until="domcontentloaded")
        # Wacht op de rekeningtabel i.p.v. een vaste pauze: via de nieuwe
        # OAuth-redirectketen kan het overzicht op NAS-hardware even duren.
        try:
            await page.wait_for_selector(cfg["accounts_row_selector"], timeout=45_000)
        except Exception:
            pass  # de parser probeert het alsnog en we dumpen bij falen
        await dump(page, "step3_home")

        # ------------------------------------------------------------------
        # 4) Persist session + cookies
        # ------------------------------------------------------------------
        if save_storage_state and storage_state_path:
            try:
                await ctx.storage_state(path=storage_state_path)
            except Exception:
                pass
        if dump_cookies_path:
            try:
                await dump_cookies(ctx, dump_cookies_path)
            except Exception:
                pass

        # ------------------------------------------------------------------
        # 5) Parse the account table (primary selectors, then fallback)
        # ------------------------------------------------------------------
        accounts, primary_ok = await parse_accounts_from_page(page, cfg)
        if not primary_ok:
            await dump(page, "step4_table_not_found")
        if not accounts:
            await dump(page, "step5_no_accounts_final")

        _write_capture()
        await ctx.close()
        await browser.close()
        return accounts


# ---------------------------------------------------------------------------
# Keepalive (session warm-up without scraping)
# ---------------------------------------------------------------------------
async def keepalive_session(
    cfg: dict,
    *,
    storage_state_path: str | None = None,
    dump_cookies_path: str | None = None,
) -> bool:
    """
    Reuse stored session state to keep session alive.
    Returns True if still logged in, False otherwise.
    """
    home_url = "https://mijn.meesman.nl/"
    selector = (cfg.get("accounts_row_selector") or "").strip()
    login_selector = (cfg.get("login_user_selector") or "").strip()
    if not selector:
        return False

    playwright = await async_playwright().start()
    browser    = await playwright.chromium.launch(headless=True)

    try:
        ctx_kwargs = {}
        if storage_state_path and Path(storage_state_path).exists():
            ctx_kwargs["storage_state"] = storage_state_path

        ctx  = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)

        # Wacht tot óf de rekeningtabel (ingelogd) óf het loginformulier
        # (verlopen) verschijnt — geen vaste pauze, die was op de NAS te kort
        # en gaf valse "sessie verlopen"-meldingen met onnodige re-logins.
        wait_for = f"{selector}, {login_selector}" if login_selector else selector
        try:
            await page.wait_for_selector(wait_for, timeout=45_000)
        except Exception:
            pass

        logged_in = False
        try:
            rows = await page.query_selector_all(selector)
            logged_in = bool(rows)
        except Exception:
            pass

        if logged_in:
            if storage_state_path:
                try:
                    await ctx.storage_state(path=storage_state_path)
                except Exception:
                    pass
            if dump_cookies_path:
                try:
                    await dump_cookies(ctx, dump_cookies_path)
                except Exception:
                    pass

        await ctx.close()
        return logged_in

    finally:
        await browser.close()
        await playwright.stop()


# ---------------------------------------------------------------------------
# Lichte HTTP-sessiecheck (geen browser)
# ---------------------------------------------------------------------------
def http_session_check(storage_state_path: str) -> Optional[bool]:
    """
    Controleer met de opgeslagen cookies of de sessie nog geldig is,
    zonder een browser te starten (~95% goedkoper dan Chromium).

    Returns:
      True  — ingelogd (geen redirect naar de loginpagina)
      False — sessie verlopen (redirect naar login) of geen sessiebestand
      None  — onduidelijk (netwerkfout e.d.) → caller valt terug op de browser
    """
    try:
        p = Path(storage_state_path)
        if not p.exists():
            return False

        state = json.loads(p.read_text(encoding="utf-8"))
        cookies = state.get("cookies") or []
        if not cookies:
            return False

        s = requests.Session()
        s.headers.update({"User-Agent": _HTTP_UA})
        now_ts = datetime.now(timezone.utc).timestamp()

        for c in cookies:
            exp = c.get("expires")
            if isinstance(exp, (int, float)) and 0 < exp < now_ts:
                continue  # verlopen cookie overslaan
            try:
                s.cookies.set(
                    c.get("name"), c.get("value"),
                    domain=c.get("domain"), path=c.get("path") or "/",
                )
            except Exception:
                pass

        r = s.get("https://mijn.meesman.nl/", timeout=30, allow_redirects=True)
        final_url = (r.url or "").lower()

        if "login" in final_url:
            return False
        # Zonder geldige sessie komt er een 200 met alleen het redirect-stubje
        # terug — dat is dus "niet ingelogd", geen succes.
        if _is_redirect_stub(r.text or ""):
            return False
        if r.status_code == 200 and "mijn.meesman.nl" in final_url:
            return True
        return None

    except Exception:
        return None
