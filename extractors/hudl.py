"""
HUDL extractor.

Handles:
  1. fan.hudl.com fan page URLs  (public)
  2. vcloud.hudl.com embed URLs  (public)
  3. Direct m3u8 URLs            (public)
  4. app.hudl.com watch URLs     (requires login cookies)
"""

import os
import re
import base64
import requests
from urllib.parse import urlparse, parse_qs, urljoin, unquote

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError, run_playwright_sync

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Referer": "https://www.hudl.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.hudl.com",
}

GRAPHQL_ENDPOINT = "https://www.hudl.com/api/public/graphql/query"
GRAPHQL_QUERY = (
    "query GetBroadcast($bid: ID) {"
    "  broadcast(broadcastId: $bid) {"
    "    id internalId title status embedCodeSrc siteTitle"
    "  }"
    "}"
)


class HudlExtractor(BaseExtractor):
    PLATFORM = "hudl"

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return (
            "hudl.com" in host
            or "blueframetech.com" in host
            or url.endswith(".m3u8")
            or ".m3u8?" in url
        )

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        headers = DEFAULT_HEADERS.copy()
        if isinstance(cookies, dict):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        url_type = self._identify_type(url)

        if url_type == "direct_m3u8":
            return self._extract_direct_m3u8(url, headers)
        elif url_type == "fan_page":
            return self._extract_fan_page(url, headers)
        elif url_type == "vcloud_embed":
            return self._extract_vcloud_embed(url, headers)
        elif url_type == "app_hudl":
            return self._extract_app_hudl(url, headers, cookies, session_token)
        else:
            return self._scrape_page(url, headers)

    def _identify_type(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or ""

        if url.endswith(".m3u8") or ".m3u8?" in url:
            return "direct_m3u8"
        if "fan.hudl.com" in host and "/watch" in parsed.path:
            return "fan_page"
        if "vcloud" in host and "/broadcast/" in parsed.path:
            return "vcloud_embed"
        # app.hudl.com/watch and www.hudl.com/video are both authenticated URLs
        # that require login credentials. Treat them the same.
        auth_hosts = ("app.hudl.com", "www.hudl.com", "hudl.com")
        if host in auth_hosts and re.search(r'/(watch|video)/', parsed.path):
            return "app_hudl"
        if "hudl.com" in host:
            return "hudl_page"
        return "direct_m3u8"

    # ── Fan page (fan.hudl.com) ──────────────────────────────────────

    def _extract_fan_page(self, url: str, headers: dict) -> ExtractResult:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        broadcast_b64 = params.get("b", [None])[0]
        if not broadcast_b64:
            raise ExtractionError(f"No broadcast ID in HUDL fan page URL: {url}")

        broadcast_id = self._decode_broadcast_id(broadcast_b64)
        title = "hudl_broadcast"

        # GraphQL to get title
        try:
            gql_headers = {
                **headers,
                "Content-Type": "application/json",
                "Referer": url,
                "Origin": "https://fan.hudl.com",
            }
            resp = requests.post(
                GRAPHQL_ENDPOINT,
                json={"query": GRAPHQL_QUERY, "variables": {"bid": broadcast_b64}},
                headers=gql_headers,
                timeout=15,
            )
            if resp.ok:
                broadcast = resp.json().get("data", {}).get("broadcast") or {}
                t = broadcast.get("title", "")
                if t:
                    title = _clean_title(t)
                internal = broadcast.get("internalId")
                if internal:
                    broadcast_id = internal
        except Exception:
            pass

        # VMAP API for m3u8
        try:
            m3u8_url, base_url = self._vmap_to_m3u8(broadcast_id, headers)
            return ExtractResult(
                title=title,
                platform=self.PLATFORM,
                m3u8_url=m3u8_url,
                headers={**headers, "Referer": "https://vcloud.hudl.com/"},
                base_url=base_url,
            )
        except Exception:
            pass

        # Fallback: construct URL
        m3u8_url = f"https://vcloud.hudl.com/file/broadcast/{broadcast_id}.m3u8?hfr=1"
        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            m3u8_url=m3u8_url,
            headers={**headers, "Referer": "https://vcloud.hudl.com/"},
            base_url="https://vcloud.hudl.com/file/broadcast/",
        )

    def _decode_broadcast_id(self, b64: str) -> str:
        try:
            padded = b64 + "=" * (4 - len(b64) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
            m = re.search(r"(\d+)", decoded)
            return m.group(1) if m else decoded
        except Exception:
            return b64

    def _vmap_to_m3u8(self, broadcast_id: str, headers: dict):
        vmap_url = f"https://vcloud.hudl.com/api/broadcast/vmap/{broadcast_id}?minify_js=1"
        vmap_headers = {
            **headers,
            "Referer": f"https://vcloud.hudl.com/broadcast/embed/{broadcast_id}",
        }
        resp = requests.get(vmap_url, headers=vmap_headers, timeout=15)
        resp.raise_for_status()

        matches = re.findall(r'(https?://[^\s"\'<>\]]+\.m3u8[^\s"\'<>\]]*)', resp.text)
        if matches:
            m3u8_url = matches[0]
            base_url = m3u8_url.rsplit("/", 1)[0] + "/"
            return m3u8_url, base_url

        raise ExtractionError(f"No m3u8 in VMAP for broadcast {broadcast_id}")

    # ── vCloud embed ─────────────────────────────────────────────────

    def _extract_vcloud_embed(self, url: str, headers: dict) -> ExtractResult:
        bid_match = re.search(r'/broadcast/(?:embed/)?(\d+)', url)

        if bid_match:
            broadcast_id = bid_match.group(1)
            try:
                m3u8_url, base_url = self._vmap_to_m3u8(broadcast_id, headers)
                return ExtractResult(
                    title="hudl_broadcast",
                    platform=self.PLATFORM,
                    m3u8_url=m3u8_url,
                    headers={**headers, "Referer": "https://vcloud.hudl.com/"},
                    base_url=base_url,
                )
            except Exception:
                pass

        # Scrape embed page
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            html = resp.text
            matches = re.findall(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', html)
            if matches:
                m3u8_url = matches[0]
                return ExtractResult(
                    title="hudl_broadcast",
                    platform=self.PLATFORM,
                    m3u8_url=m3u8_url,
                    headers=headers,
                    base_url=m3u8_url.rsplit("/", 1)[0] + "/",
                )
        except Exception:
            pass

        raise ExtractionError(f"Could not extract m3u8 from HUDL vCloud embed: {url}")

    # ── Direct m3u8 ─────────────────────────────────────────────────

    def _extract_direct_m3u8(self, url: str, headers: dict) -> ExtractResult:
        parsed = urlparse(url)
        if "vcloud" in (parsed.hostname or ""):
            headers = {**headers, "Referer": f"https://{parsed.hostname}/"}
        base_url = url.rsplit("/", 1)[0] + "/"

        path_parts = parsed.path.strip("/").split("/")
        title = "hudl_video"
        for part in reversed(path_parts):
            if part and not part.endswith(".m3u8") and len(part) > 3:
                title = part[:60]
                break

        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            m3u8_url=url,
            headers=headers,
            base_url=base_url,
        )

    # ── Generic HUDL page scrape ─────────────────────────────────────

    def _scrape_page(self, url: str, headers: dict) -> ExtractResult:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            html = resp.text
            title_m = re.search(r"<title>([^<]+)</title>", html)
            title = _clean_title(title_m.group(1)) if title_m else "hudl_video"

            matches = re.findall(r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)', html)
            if matches:
                m3u8_url = matches[0].replace("\\u0026", "&").replace("\\/", "/")
                return ExtractResult(
                    title=title,
                    platform=self.PLATFORM,
                    m3u8_url=m3u8_url,
                    headers=headers,
                    base_url=m3u8_url.rsplit("/", 1)[0] + "/",
                )
        except Exception:
            pass

        raise ExtractionError(
            f"Could not find video in HUDL page: {url}\n"
            "Try: Open page in Chrome → F12 → Network → filter 'm3u8' → copy URL"
        )

    # ── app.hudl.com authenticated ───────────────────────────────────

    def _extract_app_hudl(self, url: str, headers: dict, cookies: dict, session_token: str) -> ExtractResult:
        """
        Extract from app.hudl.com via HUDL GraphQL API.

        The ?v= parameter in the URL is the base64 videoId for the GraphQL query:
          video(videoId: "<b64>") { title playbackUrl streams { playbackUrl name } }
        Endpoint: https://www.hudl.com/api/graphql/query
        Auth: session cookies (ident + CloudFront-* cookies from logged-in Chrome)
        """
        if not cookies and not session_token:
            raise AuthRequiredError(
                "HUDL",
                "HUDL requires login credentials for this URL. "
                "Provide platform_email and platform_password in the "
                "/api/import request payload."
            )

        import urllib.parse as _up

        # Extract the base64 videoId from the ?v= URL parameter
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        video_b64_raw = params.get("v", [None])[0]
        if not video_b64_raw:
            raise ExtractionError(f"No ?v= parameter found in URL: {url}")

        # URL-decode — the decoded string is already a valid base64 videoId
        # (the %3D in the URL is the = padding character, don't re-pad)
        video_id = _up.unquote(video_b64_raw)

        # Build session with cookies
        import requests as _req
        from http.cookiejar import MozillaCookieJar
        session = _req.Session()

        if isinstance(cookies, dict):
            for k, v in cookies.items():
                session.cookies.set(k, v, domain=".hudl.com")
        elif isinstance(cookies, str) and os.path.isfile(cookies):
            jar = MozillaCookieJar()
            jar.load(cookies, ignore_discard=True, ignore_expires=True)
            session.cookies = jar

        gql_headers = {
            **headers,
            "Content-Type": "application/json",
            "Referer": "https://app.hudl.com/",
            "Origin": "https://app.hudl.com",
        }

        # Resolve the owning team/school name from the team id in the URL
        # (e.g. .../team/13520/... or ?team=13520). HUDL away-game titles are
        # just "@ Opponent", so the home team isn't in the title — this lookup
        # fills it in. Best-effort; never fatal to the extraction.
        team_id = None
        m_team = re.search(r"[?&]team=(\d+)", url) or re.search(r"/team/(\d+)", url)
        if m_team:
            team_id = m_team.group(1)
        team_name = self._fetch_team_name(session, team_id, gql_headers) if team_id else None

        # Query HUDL GraphQL for playbackUrl
        # Try GraphQL queries from most to least fields.
        # GraphQL rejects unknown fields, so we cascade: if the broad query
        # fails (schema changed), we retry with fewer fields.
        _gql_queries = [
            # Current known schema
            (
                "query GetVideo($vid: String!) {"
                "  video(videoId: $vid) {"
                "    id title playbackUrl requiresCookieAuth"
                "    streams { playbackUrl name requiresCookieAuth }"
                "  }"
                "}"
            ),
            # Possible future rename: streamUrl
            (
                "query GetVideo($vid: String!) {"
                "  video(videoId: $vid) { id title streamUrl"
                "    streams { streamUrl name } }"
                "}"
            ),
            # Possible future rename: hlsUrl
            (
                "query GetVideo($vid: String!) {"
                "  video(videoId: $vid) { id title hlsUrl"
                "    streams { hlsUrl name } }"
                "}"
            ),
            # Minimal fallback — just ask for any URL field via __typename
            (
                "query GetVideo($vid: String!) {"
                "  video(videoId: $vid) { id title"
                "    streams { name } }"
                "}"
            ),
        ]

        for gql_query in _gql_queries:
            try:
                resp = session.post(
                    "https://www.hudl.com/api/graphql/query",
                    json={"query": gql_query, "variables": {"vid": video_id}},
                    headers=gql_headers,
                    timeout=15,
                )
                body = resp.json()
                # Skip if GraphQL returned errors (unknown fields etc.)
                if body.get("errors"):
                    continue
                data = body.get("data", {}).get("video")
                if not data:
                    continue
                title = _clean_title(data.get("title") or "hudl_video")
                m3u8_url = (
                    data.get("playbackUrl")
                    or data.get("streamUrl")
                    or data.get("hlsUrl")
                )
                if not m3u8_url:
                    for stream in data.get("streams") or []:
                        candidate = (
                            stream.get("playbackUrl")
                            or stream.get("streamUrl")
                            or stream.get("hlsUrl")
                        )
                        if candidate and ".m3u8" in candidate:
                            m3u8_url = candidate
                            break
                if m3u8_url:
                    stream_headers = {**headers, "Referer": "https://app.hudl.com/"}
                    return ExtractResult(
                        title=title,
                        platform=self.PLATFORM,
                        m3u8_url=m3u8_url,
                        headers=stream_headers,
                        base_url=m3u8_url.rsplit("/", 1)[0] + "/",
                        team_name=team_name,
                    )
            except Exception:
                continue

        # GraphQL returned nothing useful (schema change or session expired).
        # Fall back to Playwright: load the page with a fresh browser session
        # so the video player fires its own m3u8 request, then intercept it.
        # Note: this requires cookies from login_with_browser() — old Chrome
        # exports may not have a valid app.hudl.com session.
        try:
            res = self._extract_app_hudl_playwright(url, cookies, headers)
            if res and not res.team_name:
                res.team_name = team_name
            return res
        except Exception as e:
            # Login succeeded (we have cookies) but neither the GraphQL API nor
            # the player exposed a stream URL. Be honest about which stage
            # failed instead of blaming the session — that misdirected days of
            # debugging onto auth when the real issue was stream resolution.
            raise ExtractionError(
                "Could not locate the video stream on this app.hudl.com page. "
                "Login worked, but neither HUDL's API nor the player exposed an "
                "HLS manifest. This usually means the URL is a page type the "
                "extractor doesn't yet handle, or the logged-in account lacks "
                "access to this specific video. Paste the exact app.hudl.com URL "
                f"and we'll add support. (detail: {e})"
            )

    def _fetch_team_name(self, session, team_id: str, gql_headers: dict):
        """Resolve a HUDL numeric team id to a human-readable team/school name
        via the HUDL GraphQL API. Returns the school's full name (e.g.
        "Ardrey Kell High School"), optionally with the squad name appended
        ("Ardrey Kell High School - Girls Varsity Soccer"). Best-effort —
        returns None on any failure so it never blocks the actual extraction.
        """
        query = (
            "query($id: String!){ team(id:$id){ name "
            "school { fullName shortName } } }"
        )
        try:
            resp = session.post(
                "https://www.hudl.com/api/graphql/query",
                json={"query": query, "variables": {"id": str(team_id)}},
                headers=gql_headers,
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            team = (body.get("data") or {}).get("team") or {}
            if not team:
                return None
            school = team.get("school") or {}
            school_name = school.get("fullName") or school.get("shortName")
            squad = team.get("name")
            if school_name and squad and squad.lower() not in school_name.lower():
                return f"{school_name} - {squad}"
            return school_name or squad or None
        except Exception:
            return None

    def _extract_app_hudl_playwright(self, url: str, cookies,
                                      headers: dict) -> ExtractResult:
        """
        Fallback: load the HUDL page in a headless browser with session cookies,
        intercept the .m3u8 network request the video player fires automatically.
        Works regardless of GraphQL schema changes.

        Dispatched to a fresh worker thread via run_playwright_sync because the
        sync Playwright API cannot run inside an asyncio event loop.
        """
        return run_playwright_sync(self._extract_app_hudl_playwright_sync, url, cookies, headers, timeout=60)

    def _extract_app_hudl_playwright_sync(self, url: str, cookies,
                                           headers: dict) -> ExtractResult:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        import time as _time

        captured = {"m3u8": None, "title": None}

        def _on_request(request):
            u = request.url
            if ".m3u8" not in u and ".mpd" not in u:
                return
            # Capture the HLS manifest the player fetches. HUDL has served
            # video from several CDN hosts over time (vd.hudl.com, cdn.hudl.com,
            # *.akamaihd.net, *.cloudfront.net, *.hudltech.com, ...), so we no
            # longer hard-code a single host — hard-coding "vd.hudl.com" was
            # causing app.hudl.com extraction to fail whenever the video was
            # served from any other CDN. Take the first manifest we see, but
            # upgrade to a master playlist if one appears.
            if captured["m3u8"] is None:
                captured["m3u8"] = u
            elif "master" in u.lower() and "master" not in captured["m3u8"].lower():
                captured["m3u8"] = u

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=headers.get("User-Agent", ""),
                viewport={"width": 1280, "height": 800},
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            # Load cookies into the browser context
            pw_cookies = _netscape_to_playwright_cookies(cookies)
            if pw_cookies:
                context.add_cookies(pw_cookies)

            page = context.new_page()
            page.on("request", _on_request)

            try:
                # Hit www.hudl.com first so the ident session cookie activates
                # across all hudl.com subdomains before loading the video page
                page.goto("https://www.hudl.com/",
                          wait_until="domcontentloaded", timeout=15000)
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                captured["title"] = page.title()

                # The player does NOT request its manifest until playback is
                # triggered, so just loading the page leaves no .m3u8 to catch
                # (the "page loaded but no .m3u8 request detected" symptom).
                # Click the play button to start the stream. Try a range of
                # selectors HUDL has used, plus a click in the player area as
                # a last resort.
                for sel in (
                    'button[aria-label*="play" i]',
                    'button[title*="play" i]',
                    '.vjs-big-play-button',
                    '.hudl-play-button, .play-button, button.play',
                    'video',
                ):
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1500):
                            el.click(timeout=2000)
                            break
                    except Exception:
                        continue

                # Wait up to 25s for the player to request the manifest.
                deadline = _time.time() + 25
                while captured["m3u8"] is None and _time.time() < deadline:
                    _time.sleep(0.3)

            except PWTimeout:
                pass
            finally:
                browser.close()

        if not captured["m3u8"]:
            raise ExtractionError(
                "Playwright fallback: page loaded but no video manifest "
                "(.m3u8/.mpd) request detected even after triggering playback. "
                "The player may use an embedded/blob stream this path can't "
                "intercept. Capturing the manifest URL from a logged-in "
                "browser's DevTools Network tab would pinpoint it."
            )

        m3u8_url = captured["m3u8"]
        title = _clean_title(captured["title"] or "hudl_video")
        stream_headers = {**headers, "Referer": "https://app.hudl.com/"}
        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            m3u8_url=m3u8_url,
            headers=stream_headers,
            base_url=m3u8_url.rsplit("/", 1)[0] + "/",
        )

    def _parse_hudl_streams(self, data: dict, source_url: str, headers: dict) -> ExtractResult:
        """Parse HUDL stream API response."""
        streams = data.get("streams") or data.get("items") or []
        title = data.get("title") or data.get("name") or "hudl_video"

        for stream in streams:
            stream_url = stream.get("url") or stream.get("src") or ""
            if ".m3u8" in stream_url:
                return ExtractResult(
                    title=_clean_title(title),
                    platform=self.PLATFORM,
                    m3u8_url=stream_url,
                    headers=headers,
                    base_url=stream_url.rsplit("/", 1)[0] + "/",
                )
            elif stream_url:
                return ExtractResult(
                    title=_clean_title(title),
                    platform=self.PLATFORM,
                    direct_url=stream_url,
                    use_ytdlp=True,
                    source_url=source_url,
                    headers=headers,
                )

        raise ExtractionError("No usable streams in HUDL API response")


def _clean_title(title: str) -> str:
    for suffix in [" | Hudl", " - Hudl", " | Hudl TV", "Hudl vCloud - "]:
        title = title.replace(suffix, "")
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "hudl_video"


def _netscape_to_playwright_cookies(cookies) -> list:
    """Convert cookies (file path or dict) to Playwright add_cookies() format."""
    if not cookies:
        return []
    result = []
    if isinstance(cookies, str) and os.path.isfile(cookies):
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar()
        try:
            jar.load(cookies, ignore_discard=True, ignore_expires=True)
        except Exception:
            return []
        import time as _t
        for c in jar:
            if "hudl.com" not in c.domain:
                continue
            entry = {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "secure": bool(c.secure),
                "httpOnly": False,
            }
            if c.expires and c.expires > _t.time():
                entry["expires"] = float(c.expires)
            result.append(entry)
    elif isinstance(cookies, dict):
        for name, value in cookies.items():
            result.append({"name": name, "value": value,
                           "domain": ".hudl.com", "path": "/"})
    return result


def _find_m3u8_in_dict(obj, depth=0) -> str:
    """Recursively search a JSON dict/list for any m3u8 URL."""
    if depth > 12:
        return None
    if isinstance(obj, str) and ".m3u8" in obj and obj.startswith("http"):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_m3u8_in_dict(v, depth + 1)
            if result:
                return result
    if isinstance(obj, list):
        for item in obj:
            result = _find_m3u8_in_dict(item, depth + 1)
            if result:
                return result
    return None
