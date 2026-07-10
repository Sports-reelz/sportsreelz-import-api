"""
PIXELLOT extractor (you.pixellot.tv / you.pixellot.link).

Reverse-engineered from the live you.pixellot.tv site: account login is
Firebase Authentication (client-side), and the authenticated backend is
you.pixellot.tv/api/v1/* — NOT api.pixellot.tv/v1 (that's a different
Pixellot product's API and returns "invalid username or password" for
You accounts regardless of credential validity).

Since there's no plain REST login endpoint (auth happens via the Firebase
JS SDK in the browser), PixellotAuthManager drives the real login form
with Playwright and captures the resulting session — the same browser-driven
approach used for HUDL/Trace, except the session that has to be persisted
and replayed is a full storage_state (cookies + localStorage), not just
cookies: Pixellot's real auth is a `firebase:authUser:...` entry in
localStorage, confirmed live (cookies alone 401 on /api/v1/users/me within
seconds of a proven-successful login).
"""

import re
import json
import time
from pathlib import Path
import requests
from urllib.parse import urlparse

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError, run_playwright_sync

PIXELLOT_YOU_BASE = "https://you.pixellot.tv"
PIXELLOT_LOGIN_PAGE = f"{PIXELLOT_YOU_BASE}/my/login/"
PIXELLOT_ME_ENDPOINT = f"{PIXELLOT_YOU_BASE}/api/v1/users/me"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


# ── Pixellot Auth Manager ─────────────────────────────────────────────────────

PIXELLOT_DIR = Path.home() / ".pixellot"
PIXELLOT_SESSIONS_FILE = PIXELLOT_DIR / "sessions.json"


class PixellotAuthManager:
    """
    Manages you.pixellot.tv account login (email + password -> session state).

    Pixellot's real session is Firebase Auth's client-side persistence (a
    `firebase:authUser:<apiKey>:[DEFAULT]` entry in localStorage) — NOT a
    cookie. Confirmed live: cookies captured right after a successful login
    (Stripe/GA/Hotjar/Zendesk-style tracking cookies, nothing auth-shaped)
    get a 401 from /api/v1/users/me seconds later when replayed with plain
    `requests`, while a Playwright storage_state carrying that localStorage
    key authenticates immediately with no re-login. So the cached/reused
    session object here is a full Playwright storage_state (cookies +
    localStorage), and it can only be validated by actually loading it into
    a browser — a plain HTTP request can't replay localStorage-driven auth.
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

    def _session_still_valid(self, storage_state: dict) -> bool:
        """Load the storage_state into a real browser and check the SPA's
        own auth call. There's no documented expiry to check locally, and no
        way to check from plain HTTP (see class docstring)."""
        if not storage_state:
            return False
        try:
            return bool(run_playwright_sync(self._check_session_sync, storage_state, timeout=30))
        except Exception:
            return False

    def _check_session_sync(self, storage_state: dict) -> bool:
        from playwright.sync_api import sync_playwright

        me_ok = {"v": False}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=_UA, storage_state=storage_state)
            page = context.new_page()

            def _on_response(resp):
                if "api/v1/users/me" in resp.url and resp.status == 200:
                    me_ok["v"] = True

            page.on("response", _on_response)
            try:
                page.goto(PIXELLOT_LOGIN_PAGE, wait_until="domcontentloaded", timeout=15000)
                deadline = time.time() + 10
                while not me_ok["v"] and time.time() < deadline:
                    page.wait_for_timeout(300)
            except Exception:
                pass
            finally:
                browser.close()
        return me_ok["v"]

    def get_session(self, email: str) -> dict | None:
        """Return a cached storage_state for this email if it still
        validates live, else drop the stale entry and return None."""
        entry = self._sessions.get(email)
        if not entry:
            return None
        storage_state = entry.get("storage_state")
        if storage_state and self._session_still_valid(storage_state):
            return storage_state
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
        resulting storage_state (cookies + localStorage — see class
        docstring for why localStorage is the part that actually matters).
        Confirms success via a live /api/v1/users/me check inside the same
        browser session (the site gives no visible error text on failed
        login, so URL/DOM state alone isn't a reliable success signal).

        Retries once with a fresh browser context before giving up — a
        Firebase-auth SPA redirect + resource reload has more timing
        variance under real network conditions (container/cloud egress)
        than it showed in local testing, and a transient hiccup on attempt
        one shouldn't cost the caller a full failed job.

        Dispatched to a fresh worker thread via run_playwright_sync because
        the sync Playwright API cannot run inside an asyncio event loop.
        """
        last_wrong_password = False
        for attempt in range(2):
            storage_state, wrong_password = run_playwright_sync(
                self._login_with_browser_sync, email, password, timeout=60,
            )
            if storage_state:
                self._sessions[email] = {"storage_state": storage_state, "cached_at": time.time()}
                self._save_sessions()
                return storage_state
            last_wrong_password = wrong_password
            if wrong_password:
                break  # a real "wrong password" signal — retrying won't help

        if last_wrong_password:
            raise ExtractionError(
                "PIXELLOT rejected the email/password for this "
                "you.pixellot.tv account. Double-check the credentials are "
                "for a you.pixellot.tv account specifically (not a Pixellot "
                "Partner/API account, which uses a different login system)."
            )
        # We could NOT confirm the credentials were wrong — the page never
        # reached a state we could read either way. Do not tell the caller
        # to "check credentials" here; that was the exact misleading message
        # that cost days of debugging on HUDL's "session may be expired"
        # error before we traced it to a completely different root cause.
        raise ExtractionError(
            "PIXELLOT login did not complete within the timeout (tried "
            "twice). This is NOT a confirmed credentials problem — the "
            "login form was submitted but the post-login confirmation "
            "never arrived in time. Likely causes: Pixellot's own "
            "anti-automation throttling on this account/IP after repeated "
            "recent attempts, or slower network latency from this server "
            "to Pixellot than in local testing. Wait a few minutes and "
            "retry; if it persists, try a different Pixellot account to "
            "rule out account-specific throttling."
        )

    def _login_with_browser_sync(self, email: str, password: str):
        """Returns (storage_state, wrong_password_bool). storage_state is
        None on any failure; wrong_password_bool is only True when the page
        gives an explicit, unambiguous "incorrect password/email" signal —
        everything else (timeout, no signal either way) leaves it False so
        the caller doesn't misreport an inconclusive result as bad creds."""
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

            wrong_password = False
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
                    # An explicit wrong-password/wrong-email error is the one
                    # case where retrying is pointless. This exact wording
                    # hasn't been directly observed (testing only used valid
                    # credentials), so this is a best-effort heuristic over
                    # common phrasing — deliberately narrow, so an unrelated
                    # page string can't cause a false "wrong password" verdict
                    # that skips the retry.
                    if not me_ok["value"]:
                        try:
                            body_text = page.locator("body").inner_text().lower()
                            if any(p in body_text for p in (
                                "incorrect password", "invalid password",
                                "invalid email or password", "wrong password",
                                "user not found", "no account found",
                            )):
                                wrong_password = True
                                break
                        except Exception:
                            pass

                # Capture the FULL storage_state (cookies + localStorage)
                # BEFORE closing the browser — the context (and its storage)
                # is destroyed once the browser closes. Cookies alone are
                # not enough: Pixellot's real session is the
                # firebase:authUser:... entry in localStorage, confirmed
                # live (see class docstring) — a cookies-only capture 401s
                # on /api/v1/users/me within seconds when replayed outside
                # the browser that captured it.
                storage_state = context.storage_state() if me_ok["value"] else None
            except PWTimeout:
                storage_state = None
            except Exception:
                storage_state = None
            finally:
                browser.close()

            return storage_state, wrong_password


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
          - https://you.pixellot.link/<short>           (Branch.io share link)
          - https://you.pixellot.tv/my/events/view/?id=<id>&type=event
          - https://www.pixellot.tv/{events,games}/<id>

        session_token: JWT bearer token from Pixellot API login.
        cookies: alternatively, a Playwright storage_state dict from
          PixellotAuthManager (cookies + localStorage — see that class's
          docstring for why localStorage is required), or a flat
          name->value cookie dict.
        """
        # PixellotAuthManager hands back a full Playwright storage_state
        # (it has to — Pixellot's real session lives in a localStorage key,
        # not a cookie, confirmed live). Pull the flat cookie view out of it
        # for the plain-HTTP paths below, but keep the full storage_state
        # for the Playwright fallback, which is the only path that can
        # actually authenticate this SPA.
        storage_state = None
        flat_cookies = cookies
        if isinstance(cookies, dict) and "origins" in cookies and "cookies" in cookies:
            storage_state = cookies
            flat_cookies = {c["name"]: c["value"] for c in storage_state.get("cookies", [])}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.pixellot.tv/",
        }

        if session_token:
            headers["Authorization"] = f"Bearer {session_token}"

        if flat_cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in flat_cookies.items())

        # NOTE: pixellot.link share links are intentionally NOT pre-resolved
        # here. They route through Branch.io (app.link), whose desktop-web
        # redirect is JS-driven and carries a one-time share token in the
        # URL fragment — a plain requests.head() only follows HTTP-level
        # redirects and stops on the Branch.io interstitial page, which has
        # no video on it. Confirmed live: letting Playwright load the
        # original .link URL directly follows the real redirect chain
        # (Branch.io -> /my/events/share/?v=...&token=... -> /my/events/view/)
        # and the player initializes correctly. So for .link URLs, skip
        # straight to the Playwright fallback below.
        parsed = urlparse(url)
        is_share_link = "pixellot.link" in (parsed.hostname or "")

        # Auth is only required for the API-based extraction path. The page
        # scrape fallback can work without credentials for some public URLs,
        # so we don't gate the whole method on auth being present.
        has_auth = bool(session_token or flat_cookies)

        # Fallback 1: scrape the page HTML for m3u8 (cheap, no browser needed).
        # Skipped for share links — the .link URL always lands on Branch.io's
        # interstitial, which never contains an m3u8 in its raw HTML (the
        # real page is only reached after a JS-driven redirect chain), so
        # this would just be a wasted request.
        page_title = None
        if not is_share_link:
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
        # Required for share URLs (you.pixellot.link/<short>) and for any
        # authenticated view, since the stream URL is always constructed
        # client-side and never appears in the initial HTML.
        try:
            result = self._extract_via_playwright(url, headers, storage_state or flat_cookies, page_title)
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
                                 session, page_title: str) -> ExtractResult:
        """
        Open the Pixellot URL in a headless Chromium tab, listen for the .m3u8
        network request fired by the player, and use that URL for the download.

        Required because Pixellot's share player builds the stream URL with
        runtime JavaScript — the m3u8 never appears in the initial HTML, so
        plain HTTP scraping always misses it.

        session: a Playwright storage_state dict from PixellotAuthManager
          (cookies + localStorage — the localStorage part is what actually
          authenticates, see PixellotAuthManager's docstring), a flat
          name->value cookie dict, or None.

        Dispatched to a fresh worker thread via run_playwright_sync because
        the sync Playwright API cannot run inside an asyncio event loop.
        """
        return run_playwright_sync(
            self._extract_via_playwright_sync, url, headers, session, page_title,
            timeout=60,
        )

    def _extract_via_playwright_sync(self, url: str, headers: dict,
                                      session, page_title: str) -> ExtractResult:
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

        is_storage_state = isinstance(session, dict) and "origins" in session and "cookies" in session

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context_kwargs = {
                "user_agent": headers.get("User-Agent", ""),
                "viewport": {"width": 1280, "height": 800},
            }
            if is_storage_state:
                # This is the path that actually authenticates the SPA —
                # Pixellot's Firebase Auth session lives in a localStorage
                # key, not a cookie, so it has to be loaded via storage_state
                # rather than context.add_cookies(). Confirmed live: a
                # cookies-only context on this same page never fires the
                # m3u8 request; a storage_state context does immediately.
                context_kwargs["storage_state"] = session
            context = browser.new_context(**context_kwargs)
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            # Legacy path: caller passed a flat cookie dict instead of a
            # storage_state (e.g. a stale on-disk cache from before this
            # fix). Attach what we have — it won't authenticate the SPA
            # (see above) but doesn't hurt for public/unauthenticated pages.
            if not is_storage_state and isinstance(session, dict):
                pw_cookies = []
                for name, value in session.items():
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

                def _try_click_play():
                    try:
                        play_btn = page.locator(
                            'button[aria-label*="play" i], .vjs-big-play-button, '
                            '.play-button, button.play'
                        ).first
                        if play_btn.is_visible(timeout=3000):
                            play_btn.click()
                    except Exception:
                        pass

                # Trigger video player initialisation by clicking the play
                # button if it's visible, then wait for the m3u8 request.
                _try_click_play()

                # Share links (.link) land on a Branch.io interstitial first;
                # its desktop-web redirect chain (interstitial -> share page
                # with token -> real event view) is JS-driven and needs real
                # time to complete + the destination SPA to hydrate before a
                # play button even exists. Confirmed live: ~20-35s end to end.
                # Give it a generous window and retry the click once partway
                # through, since the button that mattered wasn't on the page
                # at t=0.
                deadline = time.time() + 35
                clicked_again = False
                while captured["m3u8"] is None and time.time() < deadline:
                    time.sleep(0.3)
                    if not clicked_again and time.time() > deadline - 20:
                        clicked_again = True
                        _try_click_play()

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


def _clean_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "pixellot_game"
