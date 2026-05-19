"""
Multi-platform video extractor package.
Auto-detects platform from URL and delegates to the right extractor.

Supported platforms:
  - HUDL (fan.hudl.com, vcloud.hudl.com, app.hudl.com)
  - VEO  (app.veo.co)
  - YouTube / YouTube Shorts
  - TRACE (traceup.com) — session-required
  - PIXELLOT (pixellot.tv) — JWT-required
"""

from .base import ExtractResult, ExtractionError, detect_platform
from .hudl import HudlExtractor
from .veo import VeoExtractor
from .youtube import YouTubeExtractor
from .trace import TraceExtractor
from .pixellot import PixellotExtractor

# Platform registry — ordered by specificity
_EXTRACTORS = [
    HudlExtractor(),
    VeoExtractor(),
    YouTubeExtractor(),
    TraceExtractor(),
    PixellotExtractor(),
]


def extract(url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
    """
    Extract video info from any supported platform URL.

    Args:
        url: The video page URL.
        cookies: Optional dict of cookies for authenticated platforms.
        session_token: Optional bearer/JWT token for platforms that need it.

    Returns:
        ExtractResult with video URL(s), title, platform, headers.

    Raises:
        ExtractionError: If the URL is not supported or extraction fails.
    """
    url = url.strip()

    for extractor in _EXTRACTORS:
        if extractor.can_handle(url):
            return extractor.extract(url, cookies=cookies, session_token=session_token)

    raise ExtractionError(
        f"Unsupported URL: {url}\n"
        "Supported: HUDL (fan.hudl.com, app.hudl.com), VEO (app.veo.co), "
        "YouTube, TRACE (traceup.com), PIXELLOT (pixellot.tv)"
    )


def get_platform(url: str) -> str:
    """Return the platform name for a URL, or 'unknown'."""
    for extractor in _EXTRACTORS:
        if extractor.can_handle(url):
            return extractor.PLATFORM
    return "unknown"
