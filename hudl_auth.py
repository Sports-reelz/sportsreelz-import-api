"""
HUDL automatic authentication.
User provides email + password once. This module:
  1. Checks if saved cookies are still valid (local expiry check, no HTTP call)
  2. If expired/missing, logs in via headless browser first (invisible),
     falls back to visible browser only if headless is blocked
  3. Returns the cookies file path for use by the extractor
"""

import os
import json
import time
from pathlib import Path
from http.cookiejar import MozillaCookieJar

HUDL_DIR = Path.home() / ".hudl"
COOKIES_FILE = HUDL_DIR / "cookies.txt"
CREDS_FILE = HUDL_DIR / "credentials.json"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


# ── Credentials ───────────────────────────────────────────────────────────────

def save_credentials(email: str, password: str):
    HUDL_DIR.mkdir(parents=True, exist_ok=True)
    with open(CREDS_FILE, "w") as f:
        json.dump({"email": email, "password": password}, f)


def load_credentials() -> tuple:
    if CREDS_FILE.exists():
        try:
            data = json.loads(CREDS_FILE.read_text())
            return data.get("email", ""), data.get("password", "")
        except Exception:
            pass
    return "", ""


# ── Cookie validation (local only, zero HTTP calls) ───────────────────────────

def are_cookies_valid(full_check: bool = False) -> bool:
    """
    Check if saved HUDL cookies are still usable.

    fast path (default, full_check=False):
      - Reads cookies.txt locally, checks 'ident' cookie exists and hasn't expired.
      - Takes <1ms, no network.

    full_check=True:
      - Also makes one HTTP call to HUDL GraphQL to confirm the session is live.
      - Used by the 'Test Login' button only.
    """
    if not COOKIES_FILE.exists():
        return False

    try:
        jar = MozillaCookieJar()
        jar.load(str(COOKIES_FILE), ignore_discard=True, ignore_expires=True)

        ident_found = False
        for cookie in jar:
            if cookie.name == "ident":
                if cookie.expires and cookie.expires < time.time():
                    return False  # expired by timestamp
                ident_found = True
                break

        if not ident_found:
            return False

        if not full_check:
            return True  # trust local expiry — no HTTP needed

        # Full validation: confirm session is live with HUDL API
        import requests
        session = requests.Session()
        session.cookies = jar
        resp = session.post(
            "https://www.hudl.com/api/graphql/query",
            json={"query": "{ __typename }"},
            headers={"Content-Type": "application/json", "User-Agent": _UA},
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            return "data" in body and body.get("data") is not None

    except Exception:
        pass

    return False


# ── Browser login ─────────────────────────────────────────────────────────────

def login_with_browser(email: str, password: str, on_status=None) -> bool:
    """
    Log in to HUDL via browser and save session cookies.
    Tries headless (invisible) first — no window shown to user.
    Falls back to visible window only if headless is blocked by HUDL.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise RuntimeError(
            "Playwright not installed.\n"
            "Run: pip install playwright && playwright install chromium"
        )

    HUDL_DIR.mkdir(parents=True, exist_ok=True)

    last_error = None
    for attempt, headless in enumerate([True, False]):
        if on_status:
            if attempt == 0:
                on_status("Logging in to HUDL (background)...")
            else:
                on_status("Retrying login with browser window...")

        try:
            _do_browser_login(email, password, headless=headless,
                              on_status=on_status)
            return True
        except RuntimeError as e:
            last_error = e
            if headless:
                continue  # retry with visible browser
            raise  # visible browser also failed

    raise last_error or RuntimeError("HUDL login failed")


def _do_browser_login(email: str, password: str, headless: bool,
                      on_status=None):
    """Inner login — runs Playwright with given headless setting."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    # Anti-detection args for headless mode
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=launch_args)
        context = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 800},
            # Mask automation signals
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # Mask navigator.webdriver
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        try:
            page.goto("https://www.hudl.com/login",
                      wait_until="domcontentloaded", timeout=20000)

            # Email
            email_sel = ('input[type="email"], input[name="username"], '
                         'input[name="email"]')
            page.wait_for_selector(email_sel, timeout=12000)
            page.fill(email_sel, email)

            # Some flows have a Next button before showing password
            try:
                btn = page.locator(
                    'button:has-text("Next"), button:has-text("Continue")'
                ).first
                if btn.is_visible():
                    btn.click()
                    time.sleep(0.8)
            except Exception:
                pass

            # Password
            page.wait_for_selector('input[type="password"]', timeout=10000)
            page.fill('input[type="password"]', password)

            if on_status:
                on_status("Submitting HUDL login...")

            page.click('button[type="submit"]')

            # Wait until redirected away from login/identity pages
            page.wait_for_function(
                "() => !window.location.href.includes('/login') "
                "    && !window.location.href.includes('identity')",
                timeout=25000,
            )

            if on_status:
                on_status("Login successful, saving cookies...")

            _save_cookies_netscape(context.cookies())
            browser.close()

        except PWTimeout:
            browser.close()
            raise RuntimeError(
                "HUDL login timed out — "
                + ("headless blocked, retrying with visible browser..."
                   if headless else "check your email/password and try again.")
            )
        except Exception as e:
            browser.close()
            raise RuntimeError(f"HUDL login failed: {e}")


def _save_cookies_netscape(cookies: list):
    lines = ["# Netscape HTTP Cookie File\n"]
    for c in cookies:
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires", -1)
        if expires is None or expires < 0:
            expires = int(time.time()) + 86400 * 30
        else:
            expires = int(expires)
        lines.append(
            f"{domain}\t{flag}\t{c.get('path','/')}\t{secure}\t"
            f"{expires}\t{c.get('name','')}\t{c.get('value','')}\n"
        )
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def ensure_valid_cookies(email: str, password: str, on_status=None) -> str:
    """
    Ensure HUDL session cookies are valid.
    Uses local-only cookie check (no HTTP) — instant on every download start.
    Only triggers browser login when cookies are actually expired.
    """
    if are_cookies_valid(full_check=False):
        if on_status:
            on_status("HUDL: session active")
        return str(COOKIES_FILE)

    if not email or not password:
        raise ValueError(
            "HUDL credentials required.\n"
            "Enter your HUDL email and password in the settings."
        )

    login_with_browser(email, password, on_status=on_status)
    save_credentials(email, password)

    # After login, do a full HTTP check to confirm cookies actually work
    if not are_cookies_valid(full_check=True):
        raise RuntimeError(
            "Login appeared to succeed but session validation failed.\n"
            "Please check your credentials and try again."
        )

    if on_status:
        on_status("HUDL: logged in and session saved")

    return str(COOKIES_FILE)


def needs_hudl_auth(urls: list) -> bool:
    """Return True if any URL requires HUDL app authentication."""
    return any("app.hudl.com" in u for u in urls)
