"""
VEO extractor and file download, on plain `requests`.

Handles: app.veo.co/matches/{slug}/ (and a c.veocdn.com file link, downloaded as is)

VEO answers two JSON calls for a match, the match record and its video list.
Both are read here with `requests` (urllib3's default TLS), then the picked file
is downloaded from c.veocdn.com in this module as well. No account, token or
cookie is sent: public matches only.

Every request goes through _http_get, so a different transport later touches
one function.
"""

import logging
import os
import re
import shutil
import sys
import time
from urllib.parse import urlsplit

import requests
import urllib3

from downloader import DownloadProgress, _redact_sensitive
from utils import format_size
from .base import BaseExtractor, ExtractResult, ExtractionError, AuthRequiredError

log = logging.getLogger(__name__)

# probe3's CTRL headers, measured admitted by app.veo.co and by c.veocdn.com.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
API_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}
CDN_HEADERS = {"User-Agent": _UA}

API_URL = "https://app.veo.co/api/app/matches/"
API_TIMEOUT = (15, 30)
FILE_TIMEOUT = (15, 60)
CHUNK = 1024 * 1024
ATTEMPTS = 5
BACKOFF = (2, 4, 8, 16)

# The link pattern the old downloader tool accepted for VEO matches.
_MATCH_RE = re.compile(r"https?://app\.veo\.co/matches/(?P<id>[0-9A-Za-z-_]+)")
_MIME_EXT = {"video/mp4": "mp4", "video/webm": "webm"}
_CAPS = {"1080p": 1080, "720p": 720, "480p": 480}


# ── the one place a request leaves this module ──────────────────────────────

def _http_get(url, headers, *, stream=False, timeout=API_TIMEOUT):
    return requests.request("GET", url, headers=headers, stream=stream, timeout=timeout)


# ── helpers ─────────────────────────────────────────────────────────────────

def _safe(value, limit=40):
    """A header or JSON value that goes into a message: short, one line, plain characters."""
    text = re.sub(r"[^A-Za-z0-9 ._,;:=-]", "", str(value))[:limit].strip()
    return text or "-"


def _fail(message, stage, status="-"):
    err = ExtractionError(_redact_sensitive(message))
    err.stage = stage
    err.status = status
    return err


def _answer(r):
    return f"HTTP {r.status_code} ({_safe(r.headers.get('Server'))}, {_safe(r.headers.get('X-Cache'))})"


def _int(v):
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _get_retrying(url, headers, *, stream=False, timeout=API_TIMEOUT):
    """One retry, only on a connection error, a timeout or a 5xx. A 4xx is never retried."""
    for attempt in (0, 1):
        try:
            r = _http_get(url, headers, stream=stream, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt == 0:
                time.sleep(1)
                continue
            raise
        if r.status_code >= 500 and attempt == 0:
            r.close()
            time.sleep(1)
            continue
        return r


def _api_json(path, what, trace):
    url = API_URL + path
    try:
        r = _get_retrying(url, API_HEADERS)
    except requests.exceptions.RequestException as e:
        raise _fail(f"Could not reach VEO's match API: {type(e).__name__}", what, "unreachable")
    try:
        status = r.status_code
        if status != 200:
            trace.append(f"{what}: {status}")
            return status, r, None
        try:
            data = r.json()
        except ValueError:
            data = None
        trace.append(f"{what}: 200")
        return status, r, data
    finally:
        r.close()


# ── file choice ─────────────────────────────────────────────────────────────

def _host_ok(url):
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "c.veocdn.com" or host.endswith(".veocdn.com"))


def _mime(row):
    """The row's media type the way the old tool read it: parameters dropped, lower case ('video/mp4; codecs=avc1')."""
    mt = row.get("mime_type")
    return mt.partition(";")[0].strip().lower() if isinstance(mt, str) else None


def eligible_rows(rows):
    """Allowlist: https, a veocdn.com host, mp4 or webm. mp2t, images, other hosts, http and a missing url are never picked."""
    return [r for r in (rows or [])
            if isinstance(r, dict) and isinstance(r.get("url"), str) and _host_ok(r["url"])
            and _mime(r) in _MIME_EXT]


def _type_shown(mt):
    """A media type for a message: 'type/subtype' only, anything else (a link, junk) as 'unrecognised'."""
    base = mt.partition(";")[0].strip() if isinstance(mt, str) else ""
    return base if re.fullmatch(r"[A-Za-z0-9.+-]{1,20}/[A-Za-z0-9.+-]{1,40}", base) else "unrecognised"


def _first_few(values, n=3):
    vs = sorted(set(values))
    return ", ".join(vs[:n]) + (f" and {len(vs) - n} more" if len(vs) > n else "")


def _left_out(rows):
    """What the allowlist dropped, for the job's error line: counts, hosts and types, never a link."""
    no_link, hosts, types = 0, [], []
    for r in rows:
        url = r.get("url") if isinstance(r, dict) else None
        if not isinstance(url, str) or not url:
            no_link += 1
        elif not _host_ok(url):
            try:
                p = urlsplit(url)
                host = _safe(p.hostname or "-", 60) + ("" if p.scheme == "https" else f" ({_safe(p.scheme, 10)})")
            except ValueError:
                host = "-"
            hosts.append(host)
        else:
            types.append(_type_shown(r.get("mime_type")))
    parts = []
    if types:
        parts.append(f"{len(types)} of type {_first_few(types)}")
    if hosts:
        parts.append(f"{len(hosts)} not on an https veocdn.com link: {_first_few(hosts)}")
    if no_link:
        parts.append(f"{no_link} without a link")
    return "; ".join(parts)


def _format_list(rows):
    """The format list the old tool built from these rows (its field names), with the id it gave each entry."""
    out = []
    for i, r in enumerate(rows):
        h = _int(r.get("height"))
        rt = r.get("render_type")
        rt = rt if isinstance(rt, str) and rt else None
        fid = f"{rt}-{h}p" if rt and h else None
        out.append({"row": r, "ext": _MIME_EXT[_mime(r)], "height": h, "width": _int(r.get("width")),
                    "vbr": (_int(r.get("bit_rate")) or 0) // 1000 or None, "fid": fid, "index": i})
    ids = {}
    for f in out:
        f["fid"] = re.sub(r"[\s,/+\[\]()]", "_", f["fid"]) if f["fid"] else str(f["index"])
        ids.setdefault(f["fid"], []).append(f)
    for fid, group in ids.items():
        if len(group) > 1:
            for n, f in enumerate(group):
                f["fid"] = f"{fid}-{n}"
    return out


def _sort_key(f):
    """The old tool's default format sort, for formats with no codec, size or protocol difference: res, bitrate, container, id."""
    res = min([x for x in (f["height"], f["width"]) if x], default=0)
    vext = {"mp4": 6, "webm": 4}[f["ext"]]
    return (res, f["vbr"] is not None, f["vbr"] or 0, vext, f["fid"])


def _best(formats):
    # Python's sort is stable, as the old tool's was: of equal formats the last listed is the best.
    return sorted(formats, key=_sort_key)[-1]


def pick_file(rows, quality, ffmpeg_available):
    """
    The row the old downloader tool's -f selector would pick from its format list for this match. The -f strings
    are the ones multi_dl builds, including the quirk that without ffmpeg 1080p becomes <=720.
    Returns a row from `rows`, or None when no row is eligible.
    """
    formats = _format_list(eligible_rows(rows))
    if not formats:
        return None
    cap = _CAPS.get(quality)
    if not ffmpeg_available:
        # best[height<=N][ext=mp4]/best[height<=N]/best    ("best": best[ext=mp4]/best)
        if quality == "1080p":
            cap = 720
        pool = [f for f in formats if cap is None or (f["height"] is not None and f["height"] <= cap)]
        pick = [f for f in pool if f["ext"] == "mp4"] or pool or formats
    else:
        # bestvideo[height<=N]+bestaudio/best[height<=N]/best/b  (the m4a audio alternative never matches)
        pool = [f for f in formats if cap is None or (f["height"] is not None and f["height"] <= cap)]
        pick = pool or formats
    return _best(pick)["row"]


def _panorama_valid(row):
    """VEO lists a panorama file even when none exists; the old tool asked for it and dropped it on an error."""
    try:
        r = _http_get(row["url"], CDN_HEADERS, stream=True, timeout=FILE_TIMEOUT)
    except requests.exceptions.RequestException:
        return False
    try:
        return r.status_code < 400
    finally:
        r.close()


def choose_file(rows, quality, ffmpeg_available):
    rows = list(eligible_rows(rows))
    while rows:
        row = pick_file(rows, quality, ffmpeg_available)
        if row.get("render_type") == "panorama" and not _panorama_valid(row):
            rows = [r for r in rows if r is not row]
            continue
        return row
    return None


# ── the extractor ───────────────────────────────────────────────────────────

class VeoExtractor(BaseExtractor):
    PLATFORM = "veo"

    def can_handle(self, url: str) -> bool:
        host = self._host(url)
        return "veo.co" in host or "veocdn.com" in host

    def extract(self, url: str, cookies: dict = None, session_token: str = None) -> ExtractResult:
        """Public matches only. cookies and session_token are ignored on purpose: nothing is sent to VEO."""
        return self._extract(url, [])

    def _extract(self, url, trace):
        m = _MATCH_RE.match(url)
        if not m:
            if _host_ok(url):
                row = {"url": url, "mime_type": "video/mp4", "render_type": "standard"}
                return ExtractResult(title="veo_video", platform=self.PLATFORM, video_files=[row])
            raise _fail("This is not a VEO match link. Expected https://app.veo.co/matches/<match>/", "link")
        slug = m.group("id")

        status, r, meta = _api_json(slug, "match api", trace)
        if status != 200:
            raise _fail(f"VEO match API answered {_answer(r)}", "match-api", status)
        if not isinstance(meta, dict):
            raise _fail("VEO match API answered 200 but not JSON", "match-api", 200)
        if meta.get("is_accessible") is False:
            raise self._private(meta)

        status, r, vids = _api_json(slug + "/videos", "video list", trace)
        if status != 200:
            privacy = meta.get("privacy")
            if status in (401, 403) and privacy is not None and privacy != "public":
                raise self._private(meta)
            raise _fail(f"VEO video list answered {_answer(r)}", "video-list", status)
        if not isinstance(vids, list):
            raise _fail("VEO video list answered 200 but not JSON", "video-list", 200)

        rows = eligible_rows(vids)
        trace[-1] += f" files={len(rows)}"
        if not rows:
            status_note = (f"(processing_status={_safe(meta.get('processing_status'))}, "
                           f"expiration_status={_safe(meta.get('expiration_status'))})")
            if not vids:
                raise _fail(f"VEO lists no video file for this match {status_note}", "video-list", 200)
            raise _fail(f"VEO lists {len(vids)} file(s) for this match, none of them one this importer downloads "
                        f"(an https mp4 or webm file on veocdn.com): {_left_out(vids)} {status_note}",
                        "video-list", 200)

        title = meta.get("title")
        thumb = meta.get("thumbnail")
        return ExtractResult(
            title=_clean_title(title if isinstance(title, str) else "") or "veo_match",
            platform=self.PLATFORM,
            video_files=rows,
            thumbnail=thumb if isinstance(thumb, str) else None,
            duration=_int(meta.get("duration")),
        )

    @staticmethod
    def _private(meta):
        privacy = meta.get("privacy")
        shown = f"privacy={_safe(privacy, 20)}, is_accessible={_safe(meta.get('is_accessible'), 10)}"
        if isinstance(privacy, str) and privacy and privacy != "public":
            err = AuthRequiredError("VEO", _redact_sensitive(
                f"VEO says this match is not public ({shown}). "
                "Private VEO matches need a VEO login, which this importer does not do."))
            err.status = "private"
        else:
            # VEO did not say why; only what it reported is repeated.
            err = ExtractionError(_redact_sensitive(
                f"VEO marks this match as not accessible ({shown}). "
                "This importer imports only matches VEO opens to anyone."))
            err.status = "not-accessible"
        err.stage = "match-api"
        return err


def _clean_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', '_', title)
    return re.sub(r'\s+', ' ', title).strip()[:100] or "veo_match"


# ── the download ────────────────────────────────────────────────────────────

class _Retry(Exception):
    """This attempt failed in a way a new attempt can fix."""


def _content_range(value):
    m = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+)\s*", value or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _chunks(r):
    """Body pieces as they arrive (read1), so a drop keeps every byte that came before it and the resume starts there."""
    raw = r.raw
    read1 = getattr(raw, "read1", None)
    while True:
        chunk = read1(CHUNK, decode_content=True) if read1 else raw.read(CHUNK, decode_content=True)
        if not chunk:
            return
        yield chunk


def _stream_to(url, part, progress, callback, state):
    """One attempt. Appends to `part` from state['have']. Raises _Retry, or ExtractionError when retrying is pointless."""
    host = urlsplit(url).hostname or "the file host"
    headers = dict(CDN_HEADERS)
    if state["have"]:
        headers["Range"] = f"bytes={state['have']}-"
    try:
        r = _http_get(url, headers, stream=True, timeout=FILE_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise _Retry(f"could not reach {host}: {type(e).__name__}")
    try:
        code = r.status_code
        if code >= 500:
            raise _Retry(f"{host} answered HTTP {code}")
        if code not in (200, 206):
            raise ExtractionError(f"{host} answered HTTP {code}")
        if code == 206:
            cr = _content_range(r.headers.get("Content-Range"))
            if not cr or cr[0] != state["have"] or (state["total"] is not None and cr[2] != state["total"]):
                raise ExtractionError(f"{host} answered a byte range that does not continue the file")
            total = cr[2]
        else:
            if state["have"]:
                state["have"] = 0  # the Range was ignored: start again from the first byte
            try:
                total = int(r.headers["Content-Length"])
            except (KeyError, ValueError):
                total = None
            if total is None:
                raise ExtractionError(f"{host} did not say how large the file is")
        if total <= 0:
            raise ExtractionError(f"{host} answered an empty file")
        state["total"] = total
        with open(part, "r+b" if os.path.exists(part) else "wb") as f:
            f.truncate(state["have"])
            f.seek(state["have"])
            try:
                for chunk in _chunks(r):
                    f.write(chunk)
                    state["have"] += len(chunk)
                    progress.percent = round(min(state["have"] / total * 100, 99.9), 1)
                    elapsed = max(time.time() - progress.start_time, 0.001)
                    progress.speed = f"{format_size(state['have'] / elapsed)}/s"
                    if callback:
                        callback(progress)
            except (urllib3.exceptions.HTTPError, requests.exceptions.RequestException, OSError) as e:
                raise _Retry(f"{host} dropped the connection: {type(e).__name__}")
    finally:
        r.close()
    if state["have"] != total:
        raise _Retry("the file ended before its full size")


def download_file(result, output_path, quality="best", progress_callback=None):
    """Downloads the file `quality` picks from result.video_files. Never raises: failure is progress.status == 'error'."""
    progress = DownloadProgress()
    progress.output_path = output_path
    progress.start_time = time.time()
    progress.status = "downloading"
    part = output_path + ".part"
    try:
        row = choose_file(result.video_files, quality, shutil.which("ffmpeg") is not None)
        if row is None:
            raise ExtractionError("VEO has no downloadable video file for this match")
        url = row["url"]
        host = urlsplit(url).hostname or "?"
        rt, h = row.get("render_type"), row.get("height")
        log.info("VEO: picked %s %sx%s from %d file(s), downloading from %s",
                 f"{rt}-{h}p" if rt and h else "file", row.get("width") or "?", h or "?",
                 len(eligible_rows(result.video_files)), host)
        if os.path.exists(part):
            os.remove(part)
        state = {"have": 0, "total": None}
        last = ""
        for attempt in range(ATTEMPTS):
            try:
                _stream_to(url, part, progress, progress_callback, state)
                last = ""
                break
            except _Retry as e:
                last = str(e)
                if attempt + 1 < ATTEMPTS:
                    time.sleep(BACKOFF[attempt])
        if last:
            size = f" ({state['have']} of {state['total']} bytes)" if state["total"] else ""
            raise ExtractionError(f"VEO file download failed: {last}{size}")
        os.replace(part, output_path)
        log.info("VEO: downloaded %d of %d bytes", state["have"], state["total"])
        progress.status = "done"
        progress.percent = 100.0
        progress.size = format_size(os.path.getsize(output_path))
        elapsed = time.time() - progress.start_time
        progress.time_elapsed = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
    except Exception as e:  # noqa: BLE001 - the caller reads progress.status
        progress.status = "error"
        progress.error = _redact_sensitive(str(e) if isinstance(e, ExtractionError) else f"{type(e).__name__}")[:300]
    finally:
        if progress.status != "done":
            try:
                os.remove(part)
            except OSError:
                pass
    if progress_callback:
        progress_callback(progress)
    return progress


# ── self-check: python -m extractors.veo --check <match link> ───────────────

def check(url, out=print):
    """At most 2 API calls and a 1 KiB Range read of the picked file. Prints hosts and statuses only."""
    import ssl
    import urllib3
    out(f"VEO check: python {sys.version.split()[0]} {ssl.OPENSSL_VERSION} requests {requests.__version__} "
        f"urllib3 {urllib3.__version__}")
    trace = []
    try:
        result = VeoExtractor()._extract(url, trace)
    except ExtractionError as e:
        for line in trace:
            out(line)
        out(" / ".join(s.strip() for s in str(e).splitlines() if s.strip())[:400])
        out(f"RESULT FAIL {getattr(e, 'stage', 'extract')} {getattr(e, 'status', '-')}")
        return 1
    for line in trace:
        out(line)
    row = choose_file(result.video_files, "1080p", shutil.which("ffmpeg") is not None)
    host = urlsplit(row["url"]).hostname if row else "-"
    if row is None:
        out("RESULT FAIL pick -")
        return 1
    out(f"pick: {row.get('render_type') or 'file'}-{row.get('height') or '?'}p host={host}")
    try:
        headers = dict(CDN_HEADERS, Range="bytes=0-1023")
        r = _http_get(row["url"], headers, stream=True, timeout=FILE_TIMEOUT)
        try:
            code = r.status_code
            got = len(r.raw.read(1024, decode_content=True)) if code in (200, 206) else 0
        finally:
            r.close()
    except requests.exceptions.RequestException as e:
        out(f"file: {host} unreachable ({type(e).__name__})")
        out("RESULT FAIL file unreachable")
        return 1
    out(f"file: {code} host={host} read={got} bytes")
    if code not in (200, 206) or not got:
        out(f"RESULT FAIL file {code}")
        return 1
    out("RESULT OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        sys.exit(check(sys.argv[2]))
    print("usage: python -m extractors.veo --check <https://app.veo.co/matches/...>")
    sys.exit(2)
