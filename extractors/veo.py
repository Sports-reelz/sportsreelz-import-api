"""
VEO extractor.

Handles: app.veo.co/matches/{slug}/

VEO has two access tiers:
  - Public matches: accessible by URL, no auth needed
  - Private/team matches: require OAuth bearer token

yt-dlp has native VEO support, so we use it for info extraction.
VEO serves videos as direct MP4 from c.veocdn.com (CloudFront CDN).
Multiple formats: standard-1080p, standard-480p, panorama-2048p.
"""

import subprocess
import sys
import json
import re
import requests
from urllib.parse import urlparse

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError


class VeoExtractor(BaseExtractor):
    PLATFORM = "veo"

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return "veo.co" in host or "veocdn.com" in host

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """
        Extract VEO video info.

        For public matches: uses yt-dlp (native VEO extractor).
        For authenticated matches: adds Bearer token via yt-dlp headers.
        """
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--no-warnings",
            "--skip-download",
            "-j",
        ]

        if session_token:
            cmd += ["--add-header", f"Authorization:Bearer {session_token}"]

        if isinstance(cookies, str):
            cmd += ["--cookies", cookies]

        cmd.append(url)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise ExtractionError("yt-dlp not found. Install: pip install yt-dlp")
        except subprocess.TimeoutExpired:
            raise ExtractionError(f"yt-dlp timed out fetching VEO info: {url}")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "login" in stderr.lower() or "private" in stderr.lower() or "403" in stderr:
                raise AuthRequiredError(
                    "VEO",
                    "This VEO match is private. Provide your VEO OAuth bearer token.\n"
                    "Get it from: browser DevTools → Network → any API request → Authorization header."
                )
            raise ExtractionError(f"yt-dlp failed for VEO URL: {stderr[:300]}")

        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ExtractionError("yt-dlp returned invalid JSON for VEO URL")

        title = info.get("title") or "veo_match"
        title = _clean_title(title)
        thumbnail = info.get("thumbnail")
        duration = info.get("duration")

        # Find the best video format (prefer standard-1080p over panorama)
        formats = info.get("formats", [])
        best_url = None
        best_height = 0

        for fmt in formats:
            ext = fmt.get("ext", "")
            height = fmt.get("height") or 0
            fid = fmt.get("format_id", "")

            # Skip thumbnails (jpg), prefer standard view over panorama
            if ext == "jpg":
                continue
            if ext in ("mp4", "webm") and height > best_height:
                # Prefer standard view (follow-cam) over panoramic
                if "panorama" not in fid or best_height == 0:
                    best_url = fmt.get("url")
                    best_height = height

        if not best_url:
            # Fall through to yt-dlp for actual download
            return ExtractResult(
                title=title,
                platform=self.PLATFORM,
                use_ytdlp=True,
                source_url=url,
                thumbnail=thumbnail,
                duration=duration,
            )

        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            direct_url=best_url,
            use_ytdlp=True,         # Let yt-dlp handle actual download for best quality selection
            source_url=url,
            thumbnail=thumbnail,
            duration=duration,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Referer": "https://app.veo.co/",
            },
        )


def _clean_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "veo_match"
