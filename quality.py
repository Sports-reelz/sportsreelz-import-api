"""
Hudl Quality Selector
Parses master m3u8 playlists and selects the best quality stream.
"""

import re
import requests
from urllib.parse import urljoin


class StreamVariant:
    """Represents one quality variant from a master m3u8."""
    def __init__(self, url: str, bandwidth: int = 0, resolution: str = "",
                 width: int = 0, height: int = 0, codecs: str = "", name: str = ""):
        self.url = url
        self.bandwidth = bandwidth
        self.resolution = resolution
        self.width = width
        self.height = height
        self.codecs = codecs
        self.name = name or self._auto_name()

    def _auto_name(self) -> str:
        """Generate a human-readable name for this variant."""
        parts = []
        if self.height:
            parts.append(f"{self.height}p")
        if self.codecs:
            if "hvc1" in self.codecs or "hevc" in self.codecs.lower():
                parts.append("HEVC")
            elif "avc1" in self.codecs:
                parts.append("H.264")
        if self.bandwidth:
            mbps = self.bandwidth / 1_000_000
            parts.append(f"{mbps:.1f} Mbps")
        return " | ".join(parts) if parts else "Unknown"

    def __repr__(self):
        return f"StreamVariant({self.name}, {self.url[:60]}...)"


def parse_master_m3u8(content: str, base_url: str) -> list:
    """
    Parse a master m3u8 playlist and return a list of StreamVariants.
    Returns empty list if this is a media playlist (not a master).
    """
    lines = content.strip().split("\n")
    variants = []

    if not any("#EXT-X-STREAM-INF" in line for line in lines):
        # This is a media playlist, not a master — single quality
        return []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_stream_inf(line)

            # Next non-comment line is the URL
            i += 1
            while i < len(lines) and lines[i].strip().startswith("#"):
                i += 1

            if i < len(lines):
                stream_url = lines[i].strip()
                if not stream_url.startswith("http"):
                    stream_url = urljoin(base_url, stream_url)

                resolution = attrs.get("RESOLUTION", "")
                width, height = 0, 0
                if "x" in resolution:
                    try:
                        width, height = map(int, resolution.split("x"))
                    except ValueError:
                        pass

                variant = StreamVariant(
                    url=stream_url,
                    bandwidth=int(attrs.get("BANDWIDTH", 0)),
                    resolution=resolution,
                    width=width,
                    height=height,
                    codecs=attrs.get("CODECS", ""),
                )
                variants.append(variant)
        i += 1

    # Sort by height desc, then bandwidth desc
    variants.sort(key=lambda v: (v.height, v.bandwidth), reverse=True)
    return variants


def _parse_stream_inf(line: str) -> dict:
    """Parse attributes from an #EXT-X-STREAM-INF line."""
    attrs = {}
    # Remove the tag prefix
    attr_str = line.split(":", 1)[1] if ":" in line else ""

    # Parse key=value pairs (handles quoted values)
    pattern = r'([A-Z-]+)=(?:"([^"]+)"|([^,]+))'
    for match in re.finditer(pattern, attr_str):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[key] = value

    return attrs


def fetch_and_select(m3u8_url: str, headers: dict, base_url: str = "",
                     preferred_quality: str = "best") -> tuple:
    """
    Fetch the master m3u8, parse variants, and select the best one.

    Args:
        m3u8_url: URL of the master m3u8
        headers: HTTP headers for the request
        base_url: Base URL for resolving relative paths
        preferred_quality: "best", "1080p", "720p", "540p", or "worst"

    Returns:
        (selected_url, selected_variant, all_variants)
        If it's already a media playlist, returns (m3u8_url, None, [])
    """
    if not base_url:
        base_url = m3u8_url.rsplit("/", 1)[0] + "/"

    resp = requests.get(m3u8_url, headers=headers, timeout=15)
    resp.raise_for_status()
    content = resp.text

    variants = parse_master_m3u8(content, base_url)

    if not variants:
        # Already a media playlist — use as-is
        return m3u8_url, None, []

    selected = select_variant(variants, preferred_quality)
    return selected.url, selected, variants


def select_variant(variants: list, preferred: str = "best") -> StreamVariant:
    """Select a variant based on preference."""
    if not variants:
        raise ValueError("No stream variants available")

    if preferred == "best":
        return variants[0]  # Already sorted by quality desc

    if preferred == "worst":
        return variants[-1]

    # Try to match resolution (e.g., "1080p", "720p")
    target_height = 0
    match = re.match(r"(\d+)p?", preferred)
    if match:
        target_height = int(match.group(1))

    if target_height:
        # Find closest match
        best_match = None
        best_diff = float("inf")
        for v in variants:
            diff = abs(v.height - target_height)
            if diff < best_diff:
                best_diff = diff
                best_match = v
        if best_match:
            return best_match

    # Default to best
    return variants[0]


def format_variants_table(variants: list) -> str:
    """Format variants as a readable table for display."""
    if not variants:
        return "  (single quality stream)"

    lines = []
    for i, v in enumerate(variants):
        marker = " *" if i == 0 else "  "
        codec_tag = ""
        if "hvc1" in v.codecs or "hevc" in v.codecs.lower():
            codec_tag = " [HEVC]"
        elif "avc1" in v.codecs:
            codec_tag = " [H.264]"

        mbps = v.bandwidth / 1_000_000 if v.bandwidth else 0
        lines.append(
            f"{marker} {i + 1}. {v.resolution or '?x?'} | "
            f"{mbps:.1f} Mbps{codec_tag}"
        )
    return "\n".join(lines)
