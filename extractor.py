"""
Hudl URL Extractor
Handles all Hudl URL types and extracts m3u8 manifest URLs automatically.

Supported URL types:
  1. Direct m3u8 URLs (va.hudl.com, vi.hudl.com, vcloud.hudl.com)
  2. Fan page URLs (fan.hudl.com/.../watch?b=...)
  3. vCloud broadcast embeds (vcloud.hudl.com/broadcast/embed/...)
  4. vCloud BlueFrame (vcloud.blueframetech.com/...)
"""

import re
import base64
import requests
from urllib.parse import urlparse, parse_qs, urljoin, unquote

# Default headers that mimic a real browser
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Referer": "https://www.hudl.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.hudl.com",
}

# GraphQL endpoint for extracting broadcast info from fan page URLs
GRAPHQL_ENDPOINT = "https://www.hudl.com/api/public/graphql/query"

GRAPHQL_QUERY = (
    "query GetBroadcast($bid: ID) {"
    "  broadcast(broadcastId: $bid) {"
    "    id internalId title status embedCodeSrc siteTitle"
    "  }"
    "}"
)


class ExtractResult:
    """Result of URL extraction."""
    def __init__(self, m3u8_url: str, title: str = "", headers: dict = None, base_url: str = ""):
        self.m3u8_url = m3u8_url
        self.title = title or "hudl_video"
        self.headers = headers or DEFAULT_HEADERS.copy()
        self.base_url = base_url  # base URL for resolving relative paths in m3u8

    def __repr__(self):
        return f"ExtractResult(title={self.title!r}, m3u8={self.m3u8_url[:80]}...)"


def identify_url_type(url: str) -> str:
    """Identify what type of Hudl URL this is."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # Direct m3u8 URL
    if url.endswith(".m3u8") or ".m3u8?" in url:
        return "direct_m3u8"

    # Fan page URL
    if "fan.hudl.com" in host and "/watch" in parsed.path:
        return "fan_page"

    # vCloud broadcast embed
    if "vcloud" in host and "/broadcast/" in parsed.path:
        return "vcloud_embed"

    # Generic hudl.com video page
    if "hudl.com" in host and ("/video/" in parsed.path or "/watch" in parsed.path):
        return "hudl_page"

    # If it looks like a URL, try to treat it as a page to scrape
    if host and "hudl" in host:
        return "hudl_page"

    # Could be a raw m3u8 URL without the extension visible
    if "hudl.com" in url or "blueframetech.com" in url:
        return "direct_m3u8"

    return "unknown"


def extract(url: str, custom_headers: dict = None) -> ExtractResult:
    """
    Extract m3u8 URL from any Hudl URL type.
    Returns an ExtractResult with the m3u8 URL, title, and required headers.
    """
    url = url.strip()
    url_type = identify_url_type(url)

    headers = custom_headers or DEFAULT_HEADERS.copy()

    if url_type == "direct_m3u8":
        return _extract_direct_m3u8(url, headers)
    elif url_type == "fan_page":
        return _extract_fan_page(url, headers)
    elif url_type == "vcloud_embed":
        return _extract_vcloud_embed(url, headers)
    elif url_type == "hudl_page":
        return _extract_hudl_page(url, headers)
    else:
        # Try as direct m3u8 anyway
        return _extract_direct_m3u8(url, headers)


def _extract_direct_m3u8(url: str, headers: dict) -> ExtractResult:
    """Handle direct m3u8 URLs -- just validate and return."""
    parsed = urlparse(url)

    # Build base URL for resolving relative sub-playlist paths
    base_url = url.rsplit("/", 1)[0] + "/"

    # Try to create a meaningful title from the URL
    path_parts = parsed.path.strip("/").split("/")
    title = "hudl_video"
    for part in reversed(path_parts):
        if part and not part.endswith(".m3u8") and len(part) > 3:
            title = part[:40]
            break

    # Set Referer based on the domain
    if "vcloud" in (parsed.hostname or ""):
        headers["Referer"] = f"https://{parsed.hostname}/"
    elif "hudl.com" in (parsed.hostname or ""):
        headers["Referer"] = "https://www.hudl.com/"

    return ExtractResult(
        m3u8_url=url,
        title=title,
        headers=headers,
        base_url=base_url,
    )


def _extract_fan_page(url: str, headers: dict) -> ExtractResult:
    """
    Extract m3u8 from fan.hudl.com watch page.
    Flow: URL -> base64 broadcast ID -> GraphQL API -> broadcast ID -> VMAP API -> m3u8
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # Get broadcast ID from ?b= parameter
    broadcast_b64 = params.get("b", [None])[0]
    if not broadcast_b64:
        raise ValueError(f"No broadcast ID found in fan page URL: {url}")

    # Decode the base64 broadcast ID to get numeric ID
    broadcast_id = _decode_broadcast_id(broadcast_b64)
    title = "hudl_broadcast"

    # Try GraphQL API to get broadcast title
    try:
        gql_headers = {
            **headers,
            "Content-Type": "application/json",
            "Referer": url,
            "Origin": "https://fan.hudl.com",
        }
        payload = {
            "query": GRAPHQL_QUERY,
            "variables": {"bid": broadcast_b64},
        }
        resp = requests.post(GRAPHQL_ENDPOINT, json=payload, headers=gql_headers, timeout=15)
        if resp.ok:
            data = resp.json()
            broadcast = data.get("data", {}).get("broadcast")
            if broadcast:
                bt = broadcast.get("title", "")
                if bt:
                    title = _sanitize_title(bt.strip())
                # Use internalId if available (more reliable)
                internal = broadcast.get("internalId")
                if internal:
                    broadcast_id = internal
    except Exception:
        pass

    # Get m3u8 from VMAP API (primary method)
    try:
        result = _get_m3u8_from_vmap(broadcast_id, headers)
        result.title = title
        return result
    except Exception:
        pass

    # Fallback: construct m3u8 URL directly
    m3u8_url = f"https://vcloud.hudl.com/file/broadcast/{broadcast_id}.m3u8?hfr=1"
    vcloud_headers = {**headers, "Referer": "https://vcloud.hudl.com/"}
    return ExtractResult(
        m3u8_url=m3u8_url,
        title=title,
        headers=vcloud_headers,
        base_url=f"https://vcloud.hudl.com/file/broadcast/",
    )


def _decode_broadcast_id(broadcast_b64: str) -> str:
    """Decode the base64 broadcast ID to extract the numeric ID."""
    try:
        padded = broadcast_b64 + "=" * (4 - len(broadcast_b64) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        # Extract numeric ID (e.g., "Broadcast3760131" -> "3760131")
        numeric = re.search(r"(\d+)", decoded)
        if numeric:
            return numeric.group(1)
        return decoded
    except Exception:
        return broadcast_b64


def _get_m3u8_from_vmap(broadcast_id: str, headers: dict) -> ExtractResult:
    """Get m3u8 URL via the vCloud VMAP API."""
    vmap_url = f"https://vcloud.hudl.com/api/broadcast/vmap/{broadcast_id}?minify_js=1"
    vmap_headers = {
        **headers,
        "Referer": f"https://vcloud.hudl.com/broadcast/embed/{broadcast_id}",
    }

    resp = requests.get(vmap_url, headers=vmap_headers, timeout=15)
    resp.raise_for_status()

    # Find m3u8 URL in the VMAP XML response
    m3u8_matches = re.findall(r'(https?://[^\s"\'<>\]]+\.m3u8[^\s"\'<>\]]*)', resp.text)
    if m3u8_matches:
        m3u8_url = m3u8_matches[0]
        base_url = m3u8_url.rsplit("/", 1)[0] + "/"
        return ExtractResult(
            m3u8_url=m3u8_url,
            title="hudl_broadcast",
            headers={**headers, "Referer": "https://vcloud.hudl.com/"},
            base_url=base_url,
        )

    raise ValueError(f"No m3u8 found in VMAP response for broadcast {broadcast_id}")


def _extract_vcloud_embed(url: str, headers: dict) -> ExtractResult:
    """Extract m3u8 from vCloud broadcast embed page."""
    # Extract broadcast ID from URL path
    bid_match = re.search(r'/broadcast/(?:embed/)?(\d+)', url)

    if bid_match:
        broadcast_id = bid_match.group(1)
        # Try VMAP API first (most reliable)
        try:
            return _get_m3u8_from_vmap(broadcast_id, headers)
        except Exception:
            pass

    # Fallback: scrape the embed page
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # Look for m3u8 URLs in the page
        m3u8_matches = re.findall(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', html)
        if m3u8_matches:
            m3u8_url = m3u8_matches[0]
            base_url = m3u8_url.rsplit("/", 1)[0] + "/"
            return ExtractResult(
                m3u8_url=m3u8_url,
                title="hudl_broadcast",
                headers=headers,
                base_url=base_url,
            )

        # Look for VMAP URL in the page JS
        vmap_match = re.search(r"vmap_url['\"]?\s*:\s*['\"]([^'\"]+)['\"]", html)
        if vmap_match:
            vmap_raw = vmap_match.group(1)
            # Unescape JS hex encoding
            vmap_decoded = vmap_raw.encode().decode('unicode_escape')
            vmap_url = unquote(vmap_decoded)
            resp2 = requests.get(vmap_url, headers=headers, timeout=15)
            m3u8s = re.findall(r'(https?://[^\s"\'<>\]]+\.m3u8[^\s"\'<>\]]*)', resp2.text)
            if m3u8s:
                m3u8_url = m3u8s[0]
                base_url = m3u8_url.rsplit("/", 1)[0] + "/"
                return ExtractResult(
                    m3u8_url=m3u8_url,
                    title="hudl_broadcast",
                    headers=headers,
                    base_url=base_url,
                )

        # Last fallback: construct URL from broadcast ID
        bid_match2 = re.search(r'broadcast[_/=](\d+)', html)
        if bid_match2:
            bid = bid_match2.group(1)
            m3u8_url = f"https://vcloud.hudl.com/file/broadcast/{bid}.m3u8?hfr=1"
            return ExtractResult(
                m3u8_url=m3u8_url,
                title="hudl_broadcast",
                headers=headers,
                base_url="https://vcloud.hudl.com/file/broadcast/",
            )
    except Exception:
        pass

    raise ValueError(f"Could not extract m3u8 from vCloud embed: {url}")


def _extract_hudl_page(url: str, headers: dict) -> ExtractResult:
    """Extract m3u8 from a generic hudl.com page by scraping."""
    return _scrape_page_for_m3u8(url, headers, "hudl_video")


def _scrape_page_for_m3u8(url: str, headers: dict, default_title: str) -> ExtractResult:
    """Scrape any Hudl page to find m3u8 URLs in the HTML/JS."""
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        html = resp.text

        # Try to get title from page
        title_match = re.search(r"<title>([^<]+)</title>", html)
        title = title_match.group(1).strip() if title_match else default_title

        # Find m3u8 URLs
        m3u8_matches = re.findall(r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)', html)
        if m3u8_matches:
            m3u8_url = m3u8_matches[0]
            # Unescape any JSON encoding
            m3u8_url = m3u8_url.replace("\\u0026", "&").replace("\\/", "/")
            base_url = m3u8_url.rsplit("/", 1)[0] + "/"
            return ExtractResult(
                m3u8_url=m3u8_url,
                title=_sanitize_title(title),
                headers=headers,
                base_url=base_url,
            )
    except Exception:
        pass

    raise ValueError(
        f"Could not find m3u8 URL in page: {url}\n"
        "Try providing the direct m3u8 URL instead.\n"
        "Tip: Open the video in Chrome -> F12 -> Network tab -> filter 'm3u8' -> copy the URL"
    )


def _sanitize_title(title: str) -> str:
    """Clean up a title for use as a filename."""
    # Remove common suffixes
    for suffix in [" | Hudl", " - Hudl", " | Hudl TV", " - Hudl TV",
                   "Hudl vCloud - "]:
        title = title.replace(suffix, "")
    # Replace invalid filename chars
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title[:100] if title else "hudl_video"
