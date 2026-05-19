"""
YouTube extractor.

Uses yt-dlp under the hood — handles:
  - Regular YouTube videos (youtube.com/watch?v=...)
  - YouTube Shorts (youtube.com/shorts/...)
  - youtu.be short links
  - YouTube playlists (first video only, or all if batched)

No auth required for public videos. Private videos need cookies.
"""

import subprocess
import sys
import json
import re
from urllib.parse import urlparse

from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError

YOUTUBE_DOMAINS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}


class YouTubeExtractor(BaseExtractor):
    PLATFORM = "youtube"

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return host in YOUTUBE_DOMAINS

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """
        Use yt-dlp to get video info.
        Returns an ExtractResult with use_ytdlp=True so the downloader
        lets yt-dlp handle the actual download directly.
        """
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--no-warnings",
            "--skip-download",
            "-j",                   # JSON output
            "--no-playlist",        # Single video only
        ]

        # Add cookies if provided (path to cookies.txt or dict)
        if isinstance(cookies, str):
            # Treated as path to Netscape cookies file
            cmd += ["--cookies", cookies]

        cmd.append(url)

        try:
            result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        except FileNotFoundError:
            raise ExtractionError(
                "yt-dlp not found. Install with: pip install yt-dlp\n"
                "Or download from: https://github.com/yt-dlp/yt-dlp/releases"
            )
        except subprocess.TimeoutExpired:
            raise ExtractionError(f"yt-dlp timed out fetching YouTube info: {url}")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "private" in stderr.lower() or "login" in stderr.lower():
                raise AuthRequiredError(
                    "YouTube",
                    "This video is private. Provide a cookies.txt file from a logged-in browser."
                )
            raise ExtractionError(f"yt-dlp failed for YouTube URL: {stderr[:300]}")

        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ExtractionError("yt-dlp returned invalid JSON for YouTube URL")

        title = info.get("title") or info.get("id") or "youtube_video"
        title = _clean_title(title)
        thumbnail = info.get("thumbnail")
        duration = info.get("duration")

        return ExtractResult(
            title=title,
            platform=self.PLATFORM,
            use_ytdlp=True,          # Downloader will call yt-dlp directly
            source_url=url,          # yt-dlp will re-resolve the best format
            thumbnail=thumbnail,
            duration=duration,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/133.0.0.0 Safari/537.36",
                "Referer": "https://www.youtube.com/",
            },
        )


def _clean_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "youtube_video"
