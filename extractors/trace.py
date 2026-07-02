"""
TRACE extractor (traceup.com).

TRACE uses:
  - Magic code authentication (email → 6-digit code → session cookie)
  - HLS streaming via API:
    https://go.traceup.com/api/teams/{teamId}/games/{gameId}/gamevideo1.hls/game_video.m3u8
  - Quality levels: video_1000k.m3u8, video_2000k.m3u8, video_3000k.m3u8
  - GameCam view (fixed elevated camera, standard 16:9)

URL patterns:
  go.traceup.com/traceid/athlete/{athleteId}/watch/{gameNum}/items/{itemId}:tracecam/
  go.traceup.com/traceid/athlete/{athleteId}/watch/{gameNum}/players
"""

import re
import time
import json
import requests
from urllib.parse import urlparse
from pathlib import Path

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError, run_playwright_sync


# ── Trace Auth Manager ────────────────────────────────────────────────────────

TRACE_DIR = Path.home() / ".trace"
TRACE_SESSIONS_FILE = TRACE_DIR / "sessions.json"

TRACE_BASE = "https://go.traceup.com"
TRACE_API = f"{TRACE_BASE}/api"

# Trace's real magic-code auth API (reverse-engineered from the live SPA).
# The flow is a clean 3-step REST sequence — no browser needed:
#   1. GET  TRACE_USER_SEARCH?email=<email>        -> resolves the user_id
#   2. POST TRACE_SEND_CODE  (email, email_type)   -> emails the 6-digit code
#   3. POST TRACE_VERIFY_CODE (user_id, code)      -> sets session cookies
# This replaces the headless-browser login, which re-submitted the email on
# the verify step and made Trace issue a NEW code (invalidating the user's).
TRACE_TEAMS = "https://teams.traceup.com/webapp"
TRACE_LAPI = "https://lapi.traceup.com/tracebot-prod"
TRACE_USER_SEARCH = f"{TRACE_LAPI}/42/users/search"
TRACE_SEND_CODE = f"{TRACE_TEAMS}/autologin-url/send"
TRACE_VERIFY_CODE = f"{TRACE_TEAMS}/users/login/by-code"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": TRACE_BASE,
    "Referer": f"{TRACE_BASE}/",
}


def _pending_state_path(email: str):
    """Path to the persisted browser session (cookies + localStorage) captured
    right after the magic code is requested. Restoring it in the verify step
    lets us resume the SAME Trace login session and enter the code without
    re-submitting the email — which would make Trace issue a new code and
    invalidate the one the user already received."""
    import hashlib
    h = hashlib.sha256((email or "").encode("utf-8")).hexdigest()[:16]
    return TRACE_DIR / f"pending_{h}.json"


class TraceAuthManager:
    """
    Manages Trace magic-code authentication.

    Flow:
      1. request_magic_code(email) → triggers email with 6-digit code
      2. submit_magic_code(email, code) → returns session cookies
      3. Session cookies are saved locally for reuse
    """

    def __init__(self):
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self._sessions = self._load_sessions()

    def _load_sessions(self) -> dict:
        if TRACE_SESSIONS_FILE.exists():
            try:
                return json.loads(TRACE_SESSIONS_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_sessions(self):
        TRACE_SESSIONS_FILE.write_text(json.dumps(self._sessions, indent=2))

    def get_session(self, email: str) -> dict | None:
        """Get saved session cookies for an email, if still valid."""
        session = self._sessions.get(email)
        if not session:
            return None
        # Check expiry (sessions typically last 30 days)
        if session.get("expires", 0) < time.time():
            del self._sessions[email]
            self._save_sessions()
            return None
        return session.get("cookies")

    def clear_session(self, email: str) -> bool:
        """Forget a saved Trace session so the next import re-triggers the
        magic-code flow. Returns True if a session existed and was removed.
        Useful for testing the verification flow without waiting 30 days, and
        in production when a player's cached session goes bad."""
        if email in self._sessions:
            del self._sessions[email]
            self._save_sessions()
            return True
        return False

    def clear_all_sessions(self) -> int:
        """Forget every saved Trace session. Returns the count removed."""
        n = len(self._sessions)
        self._sessions = {}
        self._save_sessions()
        return n

    def _resolve_user_id(self, email: str):
        """Resolve a Trace email to its numeric user_id via the public search
        endpoint. Returns the int user_id or None."""
        try:
            r = requests.get(
                TRACE_USER_SEARCH, params={"email": email},
                headers=_HEADERS, timeout=15,
            )
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                uid = data.get("id")
                if uid:
                    return int(uid)
        except Exception:
            pass
        return None

    def request_magic_code(self, email: str) -> bool:
        """
        Trigger Trace to email a magic code to the given address.

        Uses Trace's real send endpoint (autologin-url/send with
        email_type=magic-code) — the exact call the Trace SPA makes. This is a
        stateless REST POST: it sends the code without binding it to a browser
        session, so the verify step can submit the code later without
        regenerating it. The headless-browser flow is kept only as a fallback.
        """
        try:
            files = {"email": (None, email), "email_type": (None, "magic-code")}
            r = requests.post(TRACE_SEND_CODE, files=files, headers=_HEADERS, timeout=15)
            if r.status_code == 200 and (r.json() or {}).get("success"):
                return True
        except Exception:
            pass

        # Fallback: drive the real sign-in form in a browser (older path).
        if self._trigger_magic_code_via_browser(email):
            return True

        return False

    def _trigger_magic_code_via_browser(self, email: str) -> bool:
        """
        Open Trace's sign-in page in a headless browser, type the email into
        the form, and click submit. This drives Trace's UI down its own
        email-send path so the magic code goes out the same way a real user
        sign-in would trigger it.

        Returns True on successful form submission, False otherwise.

        The Playwright sync API cannot run inside an asyncio event loop
        (FastAPI's handler context), so the actual browser work is dispatched
        to a fresh worker thread via run_playwright_sync.
        """
        try:
            return run_playwright_sync(self._trigger_magic_code_via_browser_sync, email, timeout=60)
        except Exception:
            return False

    def _trigger_magic_code_via_browser_sync(self, email: str) -> bool:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            return False

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                context = browser.new_context(
                    user_agent=_UA,
                    viewport={"width": 1280, "height": 800},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = context.new_page()

                try:
                    page.goto(
                        f"{TRACE_BASE}/#/",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )

                    email_input = page.wait_for_selector(
                        'input[type="email"], input[name="email"], '
                        'input[placeholder*="email" i]',
                        timeout=10000,
                    )
                    email_input.fill(email)

                    submit = page.locator(
                        'button:has-text("Sign In"), button:has-text("Continue"), '
                        'button:has-text("Send"), button[type="submit"]'
                    ).first
                    submit.click()

                    # Wait for the SPA to advance to the code-entry step, then
                    # persist this session (cookies + localStorage) so the
                    # verify step can resume it and enter the code WITHOUT
                    # re-submitting the email. Re-submitting would make Trace
                    # send a fresh code and invalidate the one we just emailed
                    # — the root cause of the "new code generated after
                    # submission / verification timed out" failures.
                    try:
                        page.wait_for_selector(
                            'input[type="text"], input[type="tel"], input[type="number"]',
                            timeout=8000,
                        )
                    except Exception:
                        pass
                    # Give Trace's backend a moment to register the request
                    # and fire the send-email job before we close the browser.
                    time.sleep(3)
                    try:
                        TRACE_DIR.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(_pending_state_path(email)))
                    except Exception:
                        pass
                    browser.close()
                    return True
                except PWTimeout:
                    browser.close()
                    return False
                except Exception:
                    browser.close()
                    return False
        except Exception:
            return False

    def submit_magic_code(self, email: str, code: str) -> dict:
        """
        Verify the 6-digit magic code via Trace's real REST endpoint and return
        the session cookies on success.

        This is a stateless POST to users/login/by-code with the resolved
        user_id + code — it does NOT re-submit the email, so it never triggers
        a new code. That is the fix for the "a new code is generated after
        submission / verification timed out" failure.
        """
        user_id = self._resolve_user_id(email)
        if not user_id:
            raise ExtractionError(
                f"Could not resolve a Trace account for {email}. "
                "Check the email is correct and registered with Trace."
            )

        session = requests.Session()
        session.headers.update(_HEADERS)
        try:
            files = {"user_id": (None, str(user_id)), "code": (None, str(code).strip())}
            resp = session.post(TRACE_VERIFY_CODE, files=files, timeout=20)
        except Exception as e:
            raise ExtractionError(f"Trace verification request failed: {e}")

        body = {}
        try:
            body = resp.json() if resp.text else {}
        except Exception:
            body = {}

        if resp.status_code == 200 and body.get("success"):
            # Collect session cookies. by-code authenticates the requests
            # session; cookies are set on the .traceup.com domain and work
            # across go/teams/lapi subdomains the extractor uses.
            cookies = dict(session.cookies)
            data = body.get("data") or {}
            if isinstance(data, dict):
                token = data.get("token") or data.get("access_token") or data.get("session")
                if token and "token" not in cookies:
                    cookies["token"] = token
            if not cookies:
                raise ExtractionError("Trace verification succeeded but no session was returned.")
            self._sessions[email] = {
                "cookies": cookies,
                "expires": time.time() + 86400 * 30,  # 30 days
            }
            self._save_sessions()
            return cookies

        # Surface Trace's own reason (e.g. invalid_credentials / expired).
        err = (body.get("error") or {})
        reason = err.get("message") or err.get("id") or f"HTTP {resp.status_code}"
        raise ExtractionError(f"Magic code verification failed: {reason}")

    def login_with_browser(self, email: str, code: str) -> dict:
        """
        Fallback: use Playwright to complete the magic code login.
        This captures the actual session cookies from the browser.

        Dispatched to a fresh worker thread via run_playwright_sync because
        the sync Playwright API cannot run inside an asyncio event loop.
        """
        return run_playwright_sync(self._login_with_browser_sync, email, code, timeout=120)

    @staticmethod
    def _find_code_inputs(page):
        """Return a locator for the magic-code input(s) if the code-entry
        screen is showing, else None. The code screen has no email field;
        the email screen does — so a visible email input means we're NOT yet
        on the code step."""
        try:
            if page.locator('input[type="email"]').first.is_visible(timeout=1500):
                return None
        except Exception:
            pass
        try:
            page.wait_for_selector(
                'input[type="tel"], input[type="number"], input[type="text"]',
                timeout=4000,
            )
        except Exception:
            return None
        loc = page.locator('input[type="tel"], input[type="number"], input[type="text"]')
        try:
            return loc if loc.count() >= 1 else None
        except Exception:
            return None

    def _login_with_browser_sync(self, email: str, code: str) -> dict:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            raise ExtractionError(
                "Playwright required for Trace login. "
                "Install: pip install playwright && playwright install chromium"
            )

        pending = _pending_state_path(email)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx_kwargs = dict(user_agent=_UA, viewport={"width": 1280, "height": 800})
            # Restore the session captured when the code was requested so we
            # resume the SAME login flow on the code-entry step.
            if pending.exists():
                try:
                    ctx_kwargs["storage_state"] = str(pending)
                except Exception:
                    pass
            context = browser.new_context(**ctx_kwargs)
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = context.new_page()

            try:
                page.goto(f"{TRACE_BASE}/#/", wait_until="domcontentloaded", timeout=20000)

                # Preferred path: the restored session lands us on the code step,
                # so enter the code WITHOUT re-submitting the email (which would
                # invalidate the user's code by issuing a new one).
                code_inputs = self._find_code_inputs(page)

                if code_inputs is None:
                    # Fallback: session couldn't be resumed. Re-submit the email
                    # to reach the code screen. NOTE: this regenerates the code,
                    # so it only works if the just-entered code matches the newly
                    # issued one — a last resort when there's no pending session.
                    email_input = page.wait_for_selector(
                        'input[type="email"], input[name="email"], '
                        'input[placeholder*="email" i]',
                        timeout=10000,
                    )
                    email_input.fill(email)
                    page.locator(
                        'button:has-text("Sign In"), button:has-text("Continue"), '
                        'button:has-text("Send"), button[type="submit"]'
                    ).first.click()
                    time.sleep(2)
                    code_inputs = self._find_code_inputs(page)

                if code_inputs is None:
                    raise ExtractionError(
                        "Could not reach the Trace code-entry screen. "
                        "Request a fresh code and try again."
                    )

                count = code_inputs.count()
                if count >= 6:
                    for i, digit in enumerate(code[:6]):
                        code_inputs.nth(i).fill(digit)
                        time.sleep(0.1)
                elif count >= 1:
                    code_inputs.first.fill(code)

                time.sleep(1)

                try:
                    page.locator(
                        'button:has-text("Sign In"), button:has-text("Verify"), '
                        'button[type="submit"]'
                    ).first.click()
                except Exception:
                    pass

                page.wait_for_function(
                    "() => window.location.href.includes('/traceid/') || "
                    "window.location.href.includes('/home')",
                    timeout=15000,
                )

                pw_cookies = context.cookies()
                cookies = {
                    c["name"]: c["value"]
                    for c in pw_cookies
                    if "traceup.com" in c.get("domain", "")
                }

                browser.close()
                # Single-use pending session — remove it now that we're done.
                try:
                    pending.unlink()
                except Exception:
                    pass

                if cookies:
                    self._sessions[email] = {
                        "cookies": cookies,
                        "expires": time.time() + 86400 * 30,
                    }
                    self._save_sessions()
                    return cookies

                raise ExtractionError("Login succeeded but no cookies captured")

            except PWTimeout:
                browser.close()
                raise ExtractionError(
                    "Trace login timed out — the code may be expired. "
                    "Request a fresh code and try again."
                )
            except ExtractionError:
                browser.close()
                raise
            except Exception as e:
                browser.close()
                raise ExtractionError(f"Trace browser login failed: {e}")


# ── Trace URL Parser ──────────────────────────────────────────────────────────

def parse_trace_url(url: str) -> dict:
    """
    Parse a Trace URL to extract IDs.

    URL patterns:
      /traceid/athlete/{athleteId}/watch/{gameNum}/items/{itemId}:tracecam/
      /traceid/athlete/{athleteId}/watch/{gameNum}/players
      /traceid/athlete/{athleteId}/home

    Returns dict with: athlete_id, game_num, item_id (whatever is found)
    """
    parsed = urlparse(url)
    path = parsed.path

    result = {}

    # Athlete ID
    m = re.search(r'/athlete/([^/]+)', path)
    if m:
        result["athlete_id"] = m.group(1)

    # Game number (the number after /watch/)
    m = re.search(r'/watch/(\d+)', path)
    if m:
        result["game_num"] = m.group(1)

    # Item ID (the specific video/camera view)
    m = re.search(r'/items/(\d+)', path)
    if m:
        result["item_id"] = m.group(1)

    # Check query params
    params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
    if "v" in params:
        result["video_id"] = params["v"]

    return result


# ── Trace Extractor ───────────────────────────────────────────────────────────

class TraceExtractor(BaseExtractor):
    PLATFORM = "trace"

    def __init__(self):
        self._auth = TraceAuthManager()

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return "traceup.com" in host or "tracevision.com" in host

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """
        Extract Trace game video.

        cookies: session cookies from TraceAuthManager
        session_token: not used for Trace (magic code auth only)
        """
        if not cookies:
            raise AuthRequiredError(
                "TRACE",
                "TRACE requires magic code authentication.\n"
                "Use the API service: POST /api/import with platform_email to trigger login."
            )

        headers = {**_HEADERS}
        if isinstance(cookies, dict):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        parsed = parse_trace_url(url)
        game_num = parsed.get("game_num")

        if not game_num:
            raise ExtractionError(
                f"Could not extract game ID from Trace URL: {url}\n"
                "Expected format: go.traceup.com/traceid/athlete/.../watch/{gameNum}/..."
            )

        # Step 1: Resolve the game page to find teamId and gameId
        team_id, game_id = self._resolve_game_ids(url, headers, parsed)

        # Step 2: Build the m3u8 URL
        # Master playlist
        master_url = (
            f"{TRACE_API}/teams/{team_id}/games/{game_id}"
            f"/gamevideo1.hls/game_video.m3u8"
        )

        # Step 3: Fetch master playlist to find quality variants
        try:
            resp = requests.get(master_url, headers=headers, timeout=15)
            if resp.status_code != 200:
                raise ExtractionError(
                    f"Trace master playlist returned {resp.status_code}. "
                    "Session may be expired."
                )
            master_content = resp.text
        except requests.RequestException as e:
            raise ExtractionError(f"Failed to fetch Trace master playlist: {e}")

        # Step 4: Select best quality (prefer 3000k for 1080p)
        best_url = self._select_quality(master_url, master_content, headers)

        # Step 5: Extract title from the game page
        title = self._get_title(url, headers, parsed)

        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            m3u8_url=best_url,
            headers=headers,
            base_url=best_url.rsplit("/", 1)[0] + "/",
        )

    def _resolve_game_ids(self, url: str, headers: dict, parsed: dict) -> tuple:
        """
        Resolve teamId and gameId from the Trace page.
        The API URL uses a different ID format than the page URL.

        Known pattern: teams/{teamId}/games/{teamId}-{gameNum}
        """
        session = requests.Session()
        session.headers.update(headers)

        # Try to find team ID from API calls
        athlete_id = parsed.get("athlete_id", "")
        game_num = parsed.get("game_num", "")

        # Method 1: Load the athlete page and find team references
        try:
            resp = session.get(
                f"{TRACE_API}/athletes/{athlete_id}/teams",
                timeout=15,
            )
            if resp.status_code == 200:
                teams = resp.json()
                if isinstance(teams, list) and teams:
                    team_id = teams[0].get("id") or teams[0].get("teamId", "")
                    if team_id:
                        game_id = f"{team_id}-{game_num}"
                        # Verify this works
                        test_url = f"{TRACE_API}/teams/{team_id}/games/{game_id}/gamevideo1.hls/game_video.m3u8"
                        test_resp = session.head(test_url, timeout=10)
                        if test_resp.status_code == 200:
                            return team_id, game_id
        except Exception:
            pass

        # Method 2: Load the watch page and intercept API calls via HTML/JS
        try:
            resp = session.get(url, timeout=15)
            html = resp.text

            # Look for team ID in the page source
            # Patterns: "teamId":"xxx", teams/xxx/games, /api/teams/xxx
            team_matches = re.findall(
                r'(?:teamId["\s:]+|teams/)([a-zA-Z0-9]+)(?:/games)?', html
            )
            for candidate in team_matches:
                if len(candidate) >= 6 and candidate != athlete_id:
                    game_id = f"{candidate}-{game_num}"
                    test_url = f"{TRACE_API}/teams/{candidate}/games/{game_id}/gamevideo1.hls/game_video.m3u8"
                    test_resp = session.head(test_url, timeout=10)
                    if test_resp.status_code == 200:
                        return candidate, game_id
        except Exception:
            pass

        # Method 3: Try the known team ID from our exploration
        # The user found: teams/12edytrp/games/12edytrp-8509793
        # Pattern: teamId is in the URL path somewhere
        try:
            # Fetch the game page API
            resp = session.get(
                f"{TRACE_API}/athletes/{athlete_id}/games",
                timeout=15,
            )
            if resp.status_code == 200:
                games = resp.json()
                if isinstance(games, list):
                    for game in games:
                        gid = str(game.get("id", ""))
                        if game_num in gid:
                            # Extract team ID from game ID (format: teamId-gameNum)
                            parts = gid.split("-", 1)
                            if len(parts) == 2:
                                return parts[0], gid
                            # Or find teamId field
                            tid = game.get("teamId", "")
                            if tid:
                                return tid, gid
        except Exception:
            pass

        # Method 4: Brute force — try loading the page with Playwright and intercept
        try:
            return self._resolve_via_browser(url, headers)
        except Exception:
            pass

        raise ExtractionError(
            f"Could not resolve Trace team/game IDs from URL: {url}\n"
            "Try providing a direct game URL from the Trace watch page."
        )

    def _resolve_via_browser(self, url: str, headers: dict) -> tuple:
        """
        Load the Trace page in a headless browser and intercept API calls
        to capture the actual teamId and gameId.

        Dispatched to a fresh worker thread via run_playwright_sync because
        the sync Playwright API cannot run inside an asyncio event loop.
        """
        return run_playwright_sync(self._resolve_via_browser_sync, url, headers, timeout=60)

    def _resolve_via_browser_sync(self, url: str, headers: dict) -> tuple:
        from playwright.sync_api import sync_playwright

        captured = {"team_id": None, "game_id": None}

        def _on_request(request):
            u = request.url
            # Intercept: /api/teams/{teamId}/games/{gameId}/
            m = re.search(r'/api/teams/([^/]+)/games/([^/]+)/', u)
            if m and not captured["team_id"]:
                captured["team_id"] = m.group(1)
                captured["game_id"] = m.group(2)

        cookie_str = headers.get("Cookie", "")
        pw_cookies = []
        if cookie_str:
            for pair in cookie_str.split("; "):
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    pw_cookies.append({
                        "name": name, "value": value,
                        "domain": ".traceup.com", "path": "/",
                    })

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=_UA)
            if pw_cookies:
                context.add_cookies(pw_cookies)

            page = context.new_page()
            page.on("request", _on_request)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Wait for API calls
                deadline = time.time() + 10
                while not captured["team_id"] and time.time() < deadline:
                    time.sleep(0.3)
            except Exception:
                pass
            finally:
                browser.close()

        if captured["team_id"] and captured["game_id"]:
            return captured["team_id"], captured["game_id"]

        raise ExtractionError("Browser intercept failed to capture Trace game IDs")

    def _select_quality(self, master_url: str, content: str, headers: dict) -> str:
        """
        Parse master m3u8 and select best quality (prefer 3000k for 1080p).
        Only select GameCam (standard 16:9), not panoramic views.
        """
        base_url = master_url.rsplit("/", 1)[0] + "/"

        # Parse m3u8 for quality variants
        # Lines like: #EXT-X-STREAM-INF:BANDWIDTH=3000000,...\nvideo_3000k.m3u8
        lines = content.strip().split("\n")
        variants = []

        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                if i + 1 < len(lines):
                    variant_url = lines[i + 1].strip()
                    if not variant_url.startswith("http"):
                        variant_url = base_url + variant_url

                    # Extract bandwidth
                    bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                    bandwidth = int(bw_match.group(1)) if bw_match else 0

                    # Extract resolution
                    res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                    width = int(res_match.group(1)) if res_match else 0
                    height = int(res_match.group(2)) if res_match else 0

                    # Skip panoramic views (usually wider than 16:9)
                    if width > 0 and height > 0:
                        ratio = width / height
                        if ratio > 2.0:  # panoramic
                            continue

                    variants.append({
                        "url": variant_url,
                        "bandwidth": bandwidth,
                        "width": width,
                        "height": height,
                    })

        if not variants:
            # No variants found — try direct quality URLs
            for quality in ["video_3000k.m3u8", "video_2000k.m3u8", "video_1000k.m3u8"]:
                test_url = base_url + quality
                try:
                    resp = requests.head(test_url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        return test_url
                except Exception:
                    continue
            # Fall back to master
            return master_url

        # Sort by bandwidth descending, pick highest ≤ 1080p
        variants.sort(key=lambda v: v["bandwidth"], reverse=True)

        for v in variants:
            if v["height"] <= 1080 or v["height"] == 0:
                return v["url"]

        # All variants > 1080p (unlikely), just pick the lowest
        return variants[-1]["url"]

    def _get_title(self, url: str, headers: dict, parsed: dict) -> str:
        """Extract a meaningful title for the game."""
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            title_m = re.search(r"<title>([^<]+)</title>", resp.text)
            if title_m:
                title = title_m.group(1).strip()
                title = re.sub(r'\s*[|·-]\s*Trace\s*$', '', title, flags=re.I)
                if title and title.lower() != "trace":
                    return _clean_title(title)
        except Exception:
            pass

        game_num = parsed.get("game_num", "unknown")
        return f"trace_game_{game_num}"


def _clean_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "trace_match"
