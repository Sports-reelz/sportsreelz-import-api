"""
Base classes for all platform extractors.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


class ExtractionError(Exception):
    """Raised when video extraction fails."""
    pass


class AuthRequiredError(ExtractionError):
    """Raised when a platform requires authentication that wasn't provided."""
    def __init__(self, platform: str, instructions: str = ""):
        self.platform = platform
        self.instructions = instructions
        msg = f"{platform} requires authentication."
        if instructions:
            msg += f"\n{instructions}"
        super().__init__(msg)


@dataclass
class ExtractResult:
    """
    Result of a video extraction.

    For direct downloads (YouTube, VEO): direct_url is set.
    For HLS streams (HUDL, PIXELLOT): m3u8_url is set.
    """
    title: str
    platform: str

    # Video delivery
    m3u8_url: Optional[str] = None          # HLS master playlist URL
    direct_url: Optional[str] = None        # Direct MP4/video URL (no ffmpeg needed for yt-dlp)
    use_ytdlp: bool = False                 # True = let yt-dlp handle the full download

    # Source URL (for yt-dlp pass-through)
    source_url: Optional[str] = None

    # Quality variants (populated from m3u8 parsing)
    qualities: list = field(default_factory=list)

    # HTTP context
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    base_url: str = ""

    # Metadata
    duration: Optional[float] = None        # seconds
    thumbnail: Optional[str] = None

    def __repr__(self):
        url = self.m3u8_url or self.direct_url or self.source_url or "?"
        return f"ExtractResult(platform={self.platform!r}, title={self.title!r}, url={url[:60]}...)"


class BaseExtractor:
    """Abstract base class for platform extractors."""

    PLATFORM: str = "unknown"

    def can_handle(self, url: str) -> bool:
        """Return True if this extractor supports the given URL."""
        raise NotImplementedError

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """
        Extract video info from the given URL.

        Args:
            url: Video page URL.
            cookies: Optional cookies dict for authenticated requests.
            session_token: Optional bearer/JWT token.

        Returns:
            ExtractResult

        Raises:
            ExtractionError, AuthRequiredError
        """
        raise NotImplementedError

    def _host(self, url: str) -> str:
        return urlparse(url).hostname or ""


def detect_platform(url: str) -> str:
    """Detect platform from URL domain."""
    host = urlparse(url).hostname or ""
    if "hudl.com" in host or "blueframetech.com" in host:
        return "hudl"
    if "veo.co" in host or "veocdn.com" in host:
        return "veo"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "traceup.com" in host or "tracevision.com" in host:
        return "trace"
    if "pixellot.tv" in host or "pixellot.co" in host:
        return "pixellot"
    return "unknown"
