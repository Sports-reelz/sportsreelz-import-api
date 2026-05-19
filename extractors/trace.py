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

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError


# ── Trace Auth Manager ────────────────────────────────────────────────────────

TRACE_DIR = Path.home() / ".trace"
TRACE_SESSIONS_FILE = TRACE_DIR / "sessions.json"

TRACE_BASE = "https://go.traceup.com"
TRACE_API = f"{TRACE_BASE}/api"

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

    def request_magic_code(self, email: str) -> bool:
        """
        Trigger Trace to send a magic code to the given email.
        Returns True if the request was accepted.
        """
        # Trace uses a magic link login — POST email to their auth endpoint
        # The endpoint sends a 6-digit code to the email
        try:
            resp = requests.post(
                f"{TRACE_BASE}/api/auth/magic-code",
                json={"email": email},
                headers=_HEADERS,
                timeout=15,
            )
            if resp.status_code in (200, 201, 204):
                return True

            # Try alternative endpoint patterns
            for endpoint in [
                f"{TRACE_BASE}/api/auth/login",
                f"{TRACE_BASE}/api/auth/send-code",
                f"{TRACE_BASE}/api/users/magic-link",
            ]:
                resp = requests.post(
                    endpoint,
                    json={"email": email},
                    headers=_HEADERS,
                    timeout=15,
                )
                if resp.status_code in (200, 201, 204):
                    return True

        except Exception as e:
            raise ExtractionError(f"Failed to request Trace magic code: {e}")

        return False

    def submit_magic_code(self, email: str, code: str) -> dict:
        """
        Submit the 6-digit magic code. Returns session cookies on success.
        """
        session = requests.Session()
        session.headers.update(_HEADERS)

        # Try submitting the code to various possible endpoints
        endpoints = [
            (f"{TRACE_BASE}/api/auth/magic-code/verify", {"email": email, "code": code}),
            (f"{TRACE_BASE}/api/auth/verify", {"email": email, "code": code}),
            (f"{TRACE_BASE}/api/auth/login", {"email": email, "magicCode": code}),
            (f"{TRACE_BASE}/api/auth/callback", {"email": email, "token": code}),
        ]

        for url, payload in endpoints:
            try:
                resp = session.post(url, json=payload, timeout=15)
                if resp.status_code in (200, 201):
                    cookies = dict(resp.cookies)
                    if cookies:
                        # Save session
                        self._sessions[email] = {
                            "cookies": cookies,
                            "expires": time.time() + 86400 * 30,  # 30 days
                        }
                        self._save_sessions()
                        return cookies
                    # Some APIs return the token in the response body
                    body = resp.json() if resp.text else {}
                    token = body.get("token") or body.get("accessToken") or body.get("session")
                    if token:
                        cookies = {"trace_session": token}
                        self._sessions[email] = {
                            "cookies": cookies,
                            "expires": time.time() + 86400 * 30,
                        }
                        self._save_sessions()
                        return cookies
            except Exception:
                continue

        raise ExtractionError(
            "Magic code verification failed. The code may be expired or invalid."
        )

    def login_with_browser(self, email: str, code: str) -> dict:
        """
        Fallback: use Playwright to complete the magic code login.
        This captures the actual session cookies from the browser.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            raise ExtractionError(
                "Playwright required for Trace login. "
                "Install: pip install playwright && playwright install chromium"
            )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
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
                # Go to login page
                page.goto(f"{TRACE_BASE}/#/", wait_until="domcontentloaded", timeout=20000)

                # Enter email
                email_input = page.wait_for_selector(
                    'input[type="email"], input[name="email"], input[placeholder*="email" i]',
                    timeout=10000,
                )
                email_input.fill(email)

                # Click sign in / submit
                submit = page.locator(
                    'button:has-text("Sign In"), button:has-text("Continue"), '
                    'button:has-text("Send"), button[type="submit"]'
                ).first
                submit.click()
                time.sleep(2)

                # Enter magic code
                # Trace shows 6 individual input boxes for the code
                code_inputs = page.locator('input[type="text"], input[type="number"], input[type="tel"]')
                count = code_inputs.count()

                if count >= 6:
                    # Individual digit inputs
                    for i, digit in enumerate(code[:6]):
                        code_inputs.nth(i).fill(digit)
                        time.sleep(0.1)
                elif count >= 1:
                    # Single input field
                    code_inputs.first.fill(code)

                time.sleep(1)

                # Click sign in
                try:
                    sign_in = page.locator(
                        'button:has-text("Sign In"), button:has-text("Verify"), '
                        'button[type="submit"]'
                    ).first
                    sign_in.click()
                except Exception:
                    pass

                # Wait for redirect (successful login)
                page.wait_for_function(
                    "() => window.location.href.includes('/traceid/') || "
                    "window.location.href.includes('/home')",
                    timeout=15000,
                )

                # Capture cookies
                pw_cookies = context.cookies()
                cookies = {}
                for c in pw_cookies:
                    if "traceup.com" in c.get("domain", ""):
                        cookies[c["name"]] = c["value"]

                browser.close()

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
                raise ExtractionError("Trace login timed out — check the magic code")
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
        """
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
