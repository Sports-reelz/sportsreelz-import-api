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
import requests
from urllib.parse import urlparse

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError

PIXELLOT_API_BASE = "https://api.pixellot.tv/v1"
PIXELLOT_LOGIN = f"{PIXELLOT_API_BASE}/login"


class PixellotExtractor(BaseExtractor):
    PLATFORM = "pixellot"

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return "pixellot.tv" in host or "pixellot.co" in host

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """
        Extract PIXELLOT video.

        session_token: JWT bearer token from Pixellot API login.
        cookies: alternatively, browser session cookies.
        """
        if not session_token and not cookies:
            raise AuthRequiredError(
                "PIXELLOT",
                "PIXELLOT requires a JWT token.\n"
                "1. Log into your Pixellot account in Chrome\n"
                "2. F12 → Network → play a video\n"
                "3. Find a request to api.pixellot.tv → copy the Authorization header value\n"
                "4. Pass it as session_token."
            )

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

        # Try to extract video ID from URL
        parsed = urlparse(url)
        path = parsed.path.strip("/")

        # Try Pixellot API for stream info
        try:
            # Pattern: /events/{event_id} or /games/{game_id}
            event_match = re.search(r'(?:events?|games?|matches?)/([a-zA-Z0-9_-]+)', path)
            if event_match:
                event_id = event_match.group(1)
                api_url = f"{PIXELLOT_API_BASE}/events/{event_id}"
                resp = requests.get(api_url, headers=headers, timeout=15)
                if resp.ok:
                    data = resp.json()
                    return self._parse_pixellot_event(data, url, headers)
        except Exception:
            pass

        # Fallback: scrape the page for m3u8
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            html = resp.text

            m3u8_matches = re.findall(r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)', html)
            if m3u8_matches:
                m3u8_url = m3u8_matches[0].replace("\\u0026", "&").replace("\\/", "/")
                title_m = re.search(r"<title>([^<]+)</title>", html)
                title = title_m.group(1).strip() if title_m else "pixellot_game"
                return ExtractResult(
                    title=_clean_title(title),
                    platform=self.PLATFORM,
                    m3u8_url=m3u8_url,
                    headers=headers,
                    base_url=m3u8_url.rsplit("/", 1)[0] + "/",
                )
        except Exception as e:
            raise ExtractionError(f"PIXELLOT extraction failed: {e}")

        raise ExtractionError(
            f"Could not find video stream in PIXELLOT page: {url}\n"
            "The player may require JavaScript. Get the m3u8 from Chrome DevTools:\n"
            "F12 → Network → play video → filter 'm3u8' → copy URL"
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
