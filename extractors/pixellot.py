"""
PIXELLOT extractor (pixellot.tv / community.pixellot.tv).

PIXELLOT uses:
  - JWT authentication (login → get token → use token on API)
  - HLS streaming (m3u8)
  - IFrame-based embed player at pixellot-web-sdk.pixellot.tv

Status: AWAITING JWT token + API endpoint capture from client.
To complete: need Partner API credentials or browser Network capture
showing the m3u8 URL and Authorization header pattern.
"""

import re
import json
import time
import requests
from urllib.parse import urlparse, parse_qs

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError, run_playwright_sync

PIXELLOT_API_BASE = "https://api.pixellot.tv/v1"
PIXELLOT_LOGIN = f"{PIXELLOT_API_BASE}/login"


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

            # Attach existing cookies if the caller provided them
            if isinstance(cookies, dict):
                pw_cookies = []
                for name, value in cookies.items():
                    pw_cookies.append({
                        "name": name, "value": value,
                        "domain": ".pixellot.tv", "path": "/",
                    })
                if pw_cookies:
                    context.add_cookies(pw_cookies)

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
