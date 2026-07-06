"""
PIXELLOT extractor (you.pixellot.tv / you.pixellot.link).

Reverse-engineered from the live you.pixellot.tv site: account login is
Firebase Authentication (client-side), and the authenticated backend is
you.pixellot.tv/api/v1/* — NOT api.pixellot.tv/v1 (that's a different
Pixellot product's API and returns "invalid username or password" for
You accounts regardless of credential validity).

Since there's no plain REST login endpoint (auth happens via the Firebase
JS SDK in the browser), PixellotAuthManager drives the real login form
with Playwright and captures the resulting session cookies — the same
approach used for HUDL/Trace.
"""

import re
import json
import time
from pathlib import Path
import requests
from urllib.parse import urlparse, parse_qs

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError, run_playwright_sync

PIXELLOT_YOU_BASE = "https://you.pixellot.tv"
PIXELLOT_LOGIN_PAGE = f"{PIXELLOT_YOU_BASE}/my/login/"
PIXELLOT_ME_ENDPOINT = f"{PIXELLOT_YOU_BASE}/api/v1/users/me"

# Legacy/unused: api.pixellot.tv is a different Pixellot product's API and
# does not accept you.pixellot.tv account credentials. Kept only so any
# external reference to these names doesn't hard-fail on import.
PIXELLOT_API_BASE = "https://api.pixellot.tv/v1"
PIXELLOT_LOGIN = f"{PIXELLOT_API_BASE}/login"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


# ── Pixellot Auth Manager ─────────────────────────────────────────────────────

PIXELLOT_DIR = Path.home() / ".pixellot"
PIXELLOT_SESSIONS_FILE = PIXELLOT_DIR / "sessions.json"


class PixellotAuthManager:
    """
    Manages you.pixellot.tv account login (email + password -> session cookies).

    Pixellot You's session lifetime isn't documented, so rather than caching
    blindly for a fixed window (the mistake that caused the Trace regeneration
    bug), a cached session is always re-validated with one cheap GET to
    /api/v1/users/me before reuse. Only a real 200 counts as valid.
    """

    def __init__(self):
        PIXELLOT_DIR.mkdir(parents=True, exist_ok=True)
        self._sessions = self._load_sessions()

    def _load_sessions(self) -> dict:
        if PIXELLOT_SESSIONS_FILE.exists():
            try:
                return json.loads(PIXELLOT_SESSIONS_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_sessions(self):
        PIXELLOT_SESSIONS_FILE.write_text(json.dumps(self._sessions, indent=2))

    def _cookies_still_valid(self, cookies: dict) -> bool:
        """One cheap GET against a real authenticated endpoint. This is the
        only reliable way to know a Pixellot session is still good — there's
        no documented expiry to check locally."""
        if not cookies:
            return False
        try:
            r = requests.get(
                PIXELLOT_ME_ENDPOINT,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                cookies=cookies,
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def get_session(self, email: str) -> dict | None:
        """Return cached cookies for this email if they still validate live,
        else drop the stale entry and return None."""
        entry = self._sessions.get(email)
        if not entry:
            return None
        cookies = entry.get("cookies") or {}
        if self._cookies_still_valid(cookies):
            return cookies
        del self._sessions[email]
        self._save_sessions()
        return None

    def clear_session(self, email: str) -> bool:
        if email in self._sessions:
            del self._sessions[email]
            self._save_sessions()
            return True
        return False

    def login_with_browser(self, email: str, password: str) -> dict:
        """
        Log into you.pixellot.tv/my/login/ with Playwright and capture the
        resulting session cookies. Confirms success via a live
        /api/v1/users/me check inside the same browser session (the site
        gives no visible error text on failed login, so URL/DOM state alone
        isn't a reliable success signal).

        Dispatched to a fresh worker thread via run_playwright_sync because
        the sync Playwright API cannot run inside an asyncio event loop.
        """
        cookies = run_playwright_sync(
            self._login_with_browser_sync, email, password, timeout=60,
        )
        if cookies:
            self._sessions[email] = {"cookies": cookies, "cached_at": time.time()}
            self._save_sessions()
            return cookies
        raise ExtractionError(
            "PIXELLOT login failed — check the email/password are correct "
            "for a you.pixellot.tv account (not a Pixellot Partner/API "
            "account, which uses a different login system)."
        )

    def _login_with_browser_sync(self, email: str, password: str) -> dict:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 800},
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = context.new_page()

            me_ok = {"value": False}

            def _on_response(resp):
                if resp.url.startswith(PIXELLOT_ME_ENDPOINT) and resp.status == 200:
                    me_ok["value"] = True

            page.on("response", _on_response)

            try:
                page.goto(PIXELLOT_LOGIN_PAGE, wait_until="domcontentloaded", timeout=25000)
                # The login page is a Nuxt/Vue SPA — at "domcontentloaded" the
                # server-rendered HTML (including #email/#password) is present,
                # but Vue's client-side event handlers may not be bound yet.
                # Clicking LOG IN too early is a silent no-op (no error, no
                # network request, nothing) rather than a visible failure —
                # this is what caused the first version of this method to
                # fail 100% of the time despite identical selectors to a
                # manual test that included this wait.
                page.wait_for_timeout(2000)
                page.fill("#email", email)
                page.fill("#password", password)
                page.locator('button:has-text("LOG IN")').first.click()

                # After a successful login the SPA does a full client-side
                # redirect to /my/profile/ and reloads a batch of JS/CSS/image
                # chunks before it finally calls /api/v1/users/me — observed
                # to take noticeably longer than a bare form submit under
                # headless/cold-start conditions. 35s gives real margin.
                deadline = time.time() + 35
                while not me_ok["value"] and time.time() < deadline:
                    time.sleep(0.3)

                # Capture cookies BEFORE closing the browser — the context
                # (and its cookie jar) is destroyed once the browser closes.
                cookies = ({c["name"]: c["value"] for c in context.cookies()}
                           if me_ok["value"] else {})
            except PWTimeout:
                raise ExtractionError("PIXELLOT login timed out loading the sign-in page")
            except Exception as e:
                raise ExtractionError(f"PIXELLOT browser login failed: {e}")
            finally:
                browser.close()

            return cookies


class PixellotExtractor(BaseExtractor):
    PLATFORM = "pixellot"

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return ("pixellot.tv" in host
                or "pixellot.co" in host
                or "pixellot.link" in host)

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """
        Extract PIXELLOT video.

        Handles three URL families:
          - https://you.pixellot.link/<short>           (short link → 302 redirect)
          - https://you.pixellot.tv/my/events/view/?id=<id>&type=event
          - https://www.pixellot.tv/{events,games}/<id>

        session_token: JWT bearer token from Pixellot API login.
        cookies: alternatively, browser session cookies.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.pixellot.tv/",
        }

        if session_token:
            headers["Authorization"] = f"Bearer {session_token}"

        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        # Resolve short links — you.pixellot.link/<short> returns 302 to the
        # real events view URL. Follow the redirect once so the rest of the
        # extractor sees the resolved URL.
        parsed = urlparse(url)
        if "pixellot.link" in (parsed.hostname or ""):
            try:
                resolved = requests.head(
                    url, headers=headers, allow_redirects=True, timeout=15
                )
                if resolved.url and resolved.url != url:
                    url = resolved.url
                    parsed = urlparse(url)
            except Exception:
                pass

        path = parsed.path.strip("/")

        # Auth is only required for the API-based extraction path. The page
        # scrape fallback can work without credentials for some public URLs,
        # so we don't gate the whole method on auth being present.
        has_auth = bool(session_token or cookies)

        # Try Pixellot API for stream info
        try:
            # Event ID can come from two places:
            # 1. Path: /events/<id>, /games/<id>, /matches/<id>
            # 2. Query string: ?id=<id> (used by you.pixellot.tv/my/events/view/)
            event_id = None
            event_match = re.search(r'(?:events?|games?|matches?)/([a-zA-Z0-9_-]+)', path)
            if event_match:
                event_id = event_match.group(1)
            elif parsed.query:
                qparams = parse_qs(parsed.query)
                qid = qparams.get("id") or qparams.get("event_id") or qparams.get("gameId")
                if qid:
                    event_id = qid[0]

            if event_id and has_auth:
                api_url = f"{PIXELLOT_API_BASE}/events/{event_id}"
                resp = requests.get(api_url, headers=headers, timeout=15)
                if resp.ok:
                    data = resp.json()
                    return self._parse_pixellot_event(data, url, headers)
        except Exception:
            pass

        # Fallback 1: scrape the page HTML for m3u8 (cheap, no browser needed)
        page_title = None
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            html = resp.text

            title_m = re.search(r"<title>([^<]+)</title>", html)
            if title_m:
                page_title = title_m.group(1).strip()

            m3u8_matches = re.findall(r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)', html)
            if m3u8_matches:
                m3u8_url = m3u8_matches[0].replace("\\u0026", "&").replace("\\/", "/")
                return ExtractResult(
                    title=_clean_title(page_title or "pixellot_game"),
                    platform=self.PLATFORM,
                    m3u8_url=m3u8_url,
                    headers=headers,
                    base_url=m3u8_url.rsplit("/", 1)[0] + "/",
                )
        except Exception:
            # Don't raise here — fall through to the Playwright fallback,
            # which can sometimes recover from network glitches the plain
            # HTTP scrape choked on.
            pass

        # Fallback 2: load the page in headless Chromium and intercept the
        # m3u8 network request the player fires after JavaScript loads.
        # Required for short-link share URLs (you.pixellot.link/<short>) where
        # the stream URL is constructed client-side and never appears in the
        # initial HTML.
        try:
            result = self._extract_via_playwright(url, headers, cookies, page_title)
            if result is not None:
                return result
        except AuthRequiredError:
            raise
        except Exception:
            pass

        # All paths exhausted. If no auth was provided, surface AuthRequiredError
        # so the API caller knows credentials might be needed. Otherwise raise
        # a generic extraction error pointing at manual debugging.
        if not has_auth:
            raise AuthRequiredError(
                "PIXELLOT",
                "PIXELLOT video URL could not be resolved without authentication.\n"
                "If the URL is a public share link, the player may be region-gated\n"
                "or expired. Otherwise pass platform_email + platform_password in\n"
                "/api/import so the API can log into Pixellot and fetch the stream."
            )

        raise ExtractionError(
            f"Could not find video stream in PIXELLOT page: {url}\n"
            "The player may require JavaScript. Get the m3u8 from Chrome DevTools:\n"
            "F12 -> Network -> play video -> filter 'm3u8' -> copy URL"
        )

    # ── Playwright fallback (public-share URL handling) ──────────────────

    def _extract_via_playwright(self, url: str, headers: dict,
                                 cookies, page_title: str) -> ExtractResult:
        """
        Open the Pixellot URL in a headless Chromium tab, listen for the .m3u8
        network request fired by the player, and use that URL for the download.

        Required because Pixellot's share player builds the stream URL with
        runtime JavaScript — the m3u8 never appears in the initial HTML, so
        plain HTTP scraping always misses it.

        Dispatched to a fresh worker thread via run_playwright_sync because
        the sync Playwright API cannot run inside an asyncio event loop.
        """
        return run_playwright_sync(
            self._extract_via_playwright_sync, url, headers, cookies, page_title,
            timeout=60,
        )

    def _extract_via_playwright_sync(self, url: str, headers: dict,
                                      cookies, page_title: str) -> ExtractResult:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        captured = {"m3u8": None, "title": page_title}

        def _on_request(request):
            req_url = request.url
            if captured["m3u8"] is None and ".m3u8" in req_url:
                # Prefer master playlist over per-quality variants
                if "master" in req_url.lower():
                    captured["m3u8"] = req_url
                elif captured["m3u8"] is None:
                    captured["m3u8"] = req_url

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=headers.get("User-Agent", ""),
                viewport={"width": 1280, "height": 800},
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            # Attach existing cookies if the caller provided them. Pixellot's
            # session cookies were observed to come from you.pixellot.tv
            # specifically (a Nuxt app), which may or may not be scoped to
            # the parent .pixellot.tv domain — set for both so it lands
            # regardless of how strictly the original cookie was scoped.
            if isinstance(cookies, dict):
                pw_cookies = []
                for name, value in cookies.items():
                    pw_cookies.append({
                        "name": name, "value": value,
                        "domain": "you.pixellot.tv", "path": "/",
                    })
                    pw_cookies.append({
                        "name": name, "value": value,
                        "domain": ".pixellot.tv", "path": "/",
                    })
                if pw_cookies:
                    try:
                        context.add_cookies(pw_cookies)
                    except Exception:
                        pass

            page = context.new_page()
            page.on("request", _on_request)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Trigger video player initialisation by clicking the play
                # button if it's visible, then wait for the m3u8 request.
                try:
                    play_btn = page.locator(
                        'button[aria-label*="play" i], .vjs-big-play-button, '
                        '.play-button, button.play'
                    ).first
                    if play_btn.is_visible(timeout=3000):
                        play_btn.click()
                except Exception:
                    pass

                # Wait up to 20s for the player to fetch its m3u8
                deadline = time.time() + 20
                while captured["m3u8"] is None and time.time() < deadline:
                    time.sleep(0.3)

                # Grab the page title if we didn't have one yet
                if not captured["title"]:
                    try:
                        captured["title"] = page.title()
                    except Exception:
                        pass

            except PWTimeout:
                pass
            finally:
                browser.close()

        if not captured["m3u8"]:
            return None

        m3u8_url = captured["m3u8"]
        title = _clean_title(captured["title"] or "pixellot_game")
        stream_headers = {
            **headers,
            "Referer": "https://you.pixellot.tv/",
        }
        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            m3u8_url=m3u8_url,
            headers=stream_headers,
            base_url=m3u8_url.rsplit("/", 1)[0] + "/",
        )

    def _parse_pixellot_event(self, data: dict, source_url: str, headers: dict) -> ExtractResult:
        title = data.get("title") or data.get("name") or "pixellot_game"
        streams = data.get("streams") or data.get("hlsUrl") or []

        if isinstance(streams, str):
            # Single m3u8 URL
            return ExtractResult(
                title=_clean_title(title),
                platform=self.PLATFORM,
                m3u8_url=streams,
                headers=headers,
                base_url=streams.rsplit("/", 1)[0] + "/",
            )

        for stream in (streams if isinstance(streams, list) else []):
            stream_url = stream.get("url") or stream.get("hls") or ""
            if ".m3u8" in stream_url:
                return ExtractResult(
                    title=_clean_title(title),
                    platform=self.PLATFORM,
                    m3u8_url=stream_url,
                    headers=headers,
                    base_url=stream_url.rsplit("/", 1)[0] + "/",
                )

        raise ExtractionError("No HLS stream found in PIXELLOT API response")


def _clean_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "pixellot_game"
