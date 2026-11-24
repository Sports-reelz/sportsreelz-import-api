"""VEO is read and downloaded with plain `requests` (extractors/veo.py): no yt-dlp, no subprocess, no account sent
to VEO. The other platforms' paths stay as they were.

Offline: the network is faked at requests.adapters.HTTPAdapter.send (the layer under requests' own session code, so the
real session, headers and redirect handling run), and any socket use fails the test, so nothing here touches VEO or any
other host. yt-dlp is used only as the oracle for which file the old selector picked.
Run: python -m pytest tests -q
"""
import copy
import io
import json
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
import urllib3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import multi_dl  # noqa: E402
from downloader import DownloadProgress  # noqa: E402
from extractors import extract, veo  # noqa: E402
from extractors.base import AuthRequiredError, ExtractionError, ExtractResult  # noqa: E402

SLUG = "20261024-match-lpfc-n1-g200809-u19-vb741915"
MATCH = f"https://app.veo.co/matches/{SLUG}/"
API_MATCH = f"https://app.veo.co/api/app/matches/{SLUG}"
API_VIDEOS = API_MATCH + "/videos"
CDN_480 = "https://c.veocdn.com/m1/standard/machine/aa11/video-480.mp4"
CDN_1080 = "https://c.veocdn.com/m1/standard/machine/bb22/video-1080.mp4"
CDN_PANO = "https://c.veocdn.com/m1/panorama/machine/cc33/video-2048.mp4"
SIGNED_1080 = CDN_1080 + "?Policy=PLANTPOLICYaaaa&Signature=PLANTSIGbbbb&Key-Pair-Id=PLANTKPcccc"
YT = "https://www.youtube.com/watch?v=abcdefghijk"
PY_YTDLP = [sys.executable, "-m", "yt_dlp"]
PROBE3_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/157.0.0.0 Safari/537.36")
SIZE = 60000
DATA = bytes(range(256)) * (SIZE // 256 + 1)
DATA = DATA[:SIZE]


# ── the fake network ────────────────────────────────────────────────────────

class _Raw:
    """What requests keeps in Response.raw: bytes arrive in pieces; a fault raises once the pieces before it were delivered."""

    def __init__(self, body, fault_after=None):
        self.body, self.fault_after, self.pos, self.closed = body, fault_after, 0, False

    def read1(self, amt=None, decode_content=None):
        limit = len(self.body) if self.fault_after is None else min(self.fault_after, len(self.body))
        if self.pos >= limit:
            if self.fault_after is not None and self.fault_after < len(self.body):
                raise urllib3.exceptions.IncompleteRead(self.pos, len(self.body) - self.pos)
            return b""
        end = min(self.pos + (amt or 8192), limit, self.pos + 7000)    # small pieces, as a socket delivers them
        piece = self.body[self.pos:end]
        self.pos = end
        return piece

    def stream(self, amt=None, decode_content=None):
        while True:
            piece = self.read1(amt, decode_content)
            if not piece:
                return
            yield piece

    def read(self, amt=None, decode_content=None):
        return self.read1(amt, decode_content)

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


def resp(status=200, body=b"", headers=None, fault_after=None, length=True):
    r = requests.Response()
    r.status_code = status
    r.headers = requests.structures.CaseInsensitiveDict(headers or {})
    if length and "Content-Length" not in r.headers:
        r.headers["Content-Length"] = str(len(body))
    r._content = False
    r.raw = _Raw(body, fault_after)
    return r


def js(obj, status=200, **headers):
    return resp(status, json.dumps(obj).encode(), {"Content-Type": "application/json", **headers})


def cloudfront_403():
    return resp(403, b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN"><HTML>Request blocked</HTML>',
                {"Server": "CloudFront", "X-Cache": "Error from cloudfront", "Content-Type": "text/html"})


class Net:
    """Replaces HTTPAdapter.send. `routes` maps a URL (query cut) to a response, a list of responses (one per call) or a function."""

    def __init__(self):
        self.routes = {}
        self.calls = []        # (url, headers, adapter class, kwargs)

    def send(self, adapter, request, **kw):
        url = request.url
        self.calls.append({"url": url, "headers": dict(request.headers), "adapter": type(adapter), "kw": kw,
                           "method": request.method})
        key = url.split("?")[0]
        route = self.routes.get(key, self.routes.get(url))
        if route is None:
            raise AssertionError(f"unexpected request to {key}")
        if callable(route):
            route = route(request, len([c for c in self.calls if c["url"].split("?")[0] == key]))
        elif isinstance(route, list):
            n = len([c for c in self.calls if c["url"].split("?")[0] == key])
            route = route[min(n, len(route)) - 1]
        if isinstance(route, Exception):
            raise route
        route.url = url
        route.request = request
        return route

    def to(self, host_part):
        return [c for c in self.calls if host_part in c["url"]]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network use in an offline test")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for var in ("YT_DLP_COOKIES_FILE", "YOUTUBE_COOKIES_FILE", "YT_DLP_COOKIES_FROM_BROWSER",
                "BGUTIL_BASE_URL", "YT_DLP_PLAYER_CLIENT", "YT_DLP_YOUTUBE_FORMATS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda self, request, **kw: n.send(self, request, **kw))
    return n


def api_row(rt, height, mime="video/mp4", n=None):
    """One entry of VEO's /videos answer."""
    ext = mime.split("/")[1]
    return {"mime_type": mime, "url": f"https://c.veocdn.com/m1/{rt}/video-{height}{'-' + str(n) if n is not None else ''}.{ext}",
            "height": height, "width": int(height * 16 / 9), "render_type": rt, "bit_rate": 5000}


def match_json(**kw):
    return {"title": "Match - LPFC N1 G2008/09 U19", "thumbnail": "https://c.veocdn.com/m1/thumbnail.jpg",
            "duration": 5400, "is_accessible": True, "privacy": "public", "processing_status": "done",
            "expiration_status": "ok", **kw}


def serve(net, rows=None, meta=None, file_route=None):
    rows = [api_row("standard", 1080)] if rows is None else rows
    net.routes[API_MATCH] = js(meta if meta is not None else match_json())
    net.routes[API_VIDEOS] = js(rows)
    for r in rows:
        if file_route is not None:
            net.routes[r["url"]] = file_route
    return rows


def file_server(headers=None, drop_first_at=None, ignore_range=False, short_to=None, no_length=False):
    """A c.veocdn.com stand-in for one file of SIZE bytes."""
    def route(request, n):
        rng = request.headers.get("Range")
        h = {"Server": "AmazonS3", "Content-Type": "video/mp4", "Accept-Ranges": "bytes", **(headers or {})}
        if rng and not ignore_range:
            a = int(rng[6:].split("-")[0])
            body = DATA[a:]
            h["Content-Range"] = f"bytes {a}-{SIZE - 1}/{SIZE}"
            if short_to is not None:
                body = DATA[a:short_to] if a < short_to else b""
                return resp(206, body, {**h, "Content-Length": str(SIZE - a)})
            return resp(206, body, h)
        if short_to is not None:
            return resp(200, DATA[:short_to], {**h, "Content-Length": str(SIZE)})
        if drop_first_at is not None and n == 1:
            return resp(200, DATA, h, fault_after=drop_first_at)
        if no_length:
            return resp(200, DATA, h, length=False)
        return resp(200, DATA, h)
    return route


# ── STEP 2: the module and the two JSON calls ───────────────────────────────

def test_veo_module_has_no_ytdlp_and_no_subprocess():
    text = (ROOT / "extractors" / "veo.py").read_text(encoding="utf-8")
    assert re.findall(r"yt_dlp|yt-dlp|subprocess", text) == []
    assert "ssl_context" not in text and ".mount(" not in text and "Authorization" not in text


def test_api_requests_use_default_requests_tls_and_the_measured_headers(net):
    serve(net)
    r = extract(MATCH)
    assert [c["url"] for c in net.calls] == [API_MATCH, API_VIDEOS]
    for c in net.calls:
        assert c["adapter"] is requests.adapters.HTTPAdapter     # the default adapter, no custom TLS context
        assert "ssl_context" not in c["kw"] and c["method"] == "GET"
        h = {k.lower(): v for k, v in c["headers"].items()}
        assert h["user-agent"] == PROBE3_UA and h["accept"] == "application/json"
        assert not {"authorization", "referer", "cookie"} & set(h)
    assert r.platform == "veo" and r.title == "Match - LPFC N1 G2008_09 U19" and r.duration == 5400
    assert [f["url"] for f in r.video_files] == ["https://c.veocdn.com/m1/standard/video-1080.mp4"]


def test_the_cdn_request_uses_the_measured_headers_only(net, tmp_path):
    rows = serve(net, file_route=file_server())
    p = veo.download_file(extract(MATCH), str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "done"
    (c,) = net.to("c.veocdn.com")
    h = {k.lower(): v for k, v in c["headers"].items()}
    assert h["user-agent"] == PROBE3_UA
    assert not {"authorization", "referer", "cookie", "range"} & set(h)


def test_cloudfront_403_reports_the_status_and_never_says_private(net):
    net.routes[API_MATCH] = cloudfront_403()
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert not isinstance(e.value, AuthRequiredError)
    msg = str(e.value)
    assert msg.startswith("VEO match API answered HTTP 403 (CloudFront, Error from cloudfront)")
    assert "private" not in msg.lower() and "authentication" not in msg.lower()
    assert len(net.calls) == 1


def test_cloudfront_403_on_the_video_list_reports_the_status(net):
    net.routes[API_MATCH] = js(match_json())
    net.routes[API_VIDEOS] = cloudfront_403()
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value).startswith("VEO video list answered HTTP 403 (CloudFront")
    assert "private" not in str(e.value).lower()


@pytest.mark.parametrize("meta,videos_status,expect", [
    ({"is_accessible": False, "privacy": "private"}, 200, "auth"),
    ({"is_accessible": True, "privacy": "private"}, 403, "auth"),
    ({"is_accessible": False, "privacy": "public"}, 200, "not-accessible"),
    ({"is_accessible": False, "privacy": None}, 200, "not-accessible"),
    ({"is_accessible": True, "privacy": "public"}, 403, "http403"),
    ({"is_accessible": True}, 403, "http403"),
])
def test_private_only_when_the_match_json_says_so(net, meta, videos_status, expect):
    net.routes[API_MATCH] = js(match_json(**meta))
    net.routes[API_VIDEOS] = (js([api_row("standard", 1080)]) if videos_status == 200 else cloudfront_403())
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    shown = f"(privacy={meta.get('privacy')}, is_accessible={meta.get('is_accessible')})"
    if expect == "auth":
        assert isinstance(e.value, AuthRequiredError) and e.value.platform == "VEO"
        assert str(e.value).startswith("VEO requires authentication.")
        assert f"VEO says this match is not public {shown}." in str(e.value)
        assert "VEO login, which this importer does not do." in str(e.value)
    elif expect == "not-accessible":
        assert not isinstance(e.value, AuthRequiredError)
        assert str(e.value).startswith(f"VEO marks this match as not accessible {shown}.")
        assert "not public" not in str(e.value) and "login" not in str(e.value)
        assert e.value.status == "not-accessible"
    else:
        assert not isinstance(e.value, AuthRequiredError)
        assert str(e.value).startswith("VEO video list answered HTTP 403")


def test_a_match_answer_that_is_not_json_fails_closed(net):
    net.routes[API_MATCH] = resp(200, b"<html>hello</html>", {"Content-Type": "text/html"})
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value) == "VEO match API answered 200 but not JSON"
    net.routes[API_MATCH] = js(["a", "list"])
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value) == "VEO match API answered 200 but not JSON"


NONE_OF_THEM = "file(s) for this match, none of them one this importer downloads (an https mp4 or webm file on veocdn.com): "


@pytest.mark.parametrize("rows,said", [
    ([], "VEO lists no video file for this match"),
    ([api_row("standard", 480, "image/jpeg"), api_row("panorama", 2048, "video/mp2t")],
     "VEO lists 2 " + NONE_OF_THEM + "2 of type image/jpeg, video/mp2t"),
    ([{"mime_type": "video/mp4", "url": "https://cdn.example.test/v.mp4", "height": 1080, "render_type": "standard"}],
     "VEO lists 1 " + NONE_OF_THEM + "1 not on an https veocdn.com link: cdn.example.test"),
    ([{"mime_type": "video/mp4", "url": "http://c.veocdn.com/v.mp4", "height": 1080, "render_type": "standard"}],
     "VEO lists 1 " + NONE_OF_THEM + "1 not on an https veocdn.com link: c.veocdn.com (http)"),
    ([{"mime_type": "video/mp4", "height": 1080, "render_type": "standard"}],
     "VEO lists 1 " + NONE_OF_THEM + "1 without a link"),
    ([{"mime_type": "video/mp4", "url": "https://c.veocdn.com.evil.test/v.mp4", "height": 1080}],
     "VEO lists 1 " + NONE_OF_THEM + "1 not on an https veocdn.com link: c.veocdn.com.evil.test"),
    ([{"mime_type": "video/x-mp4-fragmented", "url": "https://c.veocdn.com/m1/v.mp4", "height": 1080},
      {"mime_type": "video/mp4", "url": "https://cdn.example.test/a.mp4?sig=SECRET", "height": 720}, "junk"],
     "VEO lists 3 " + NONE_OF_THEM + "1 of type video/x-mp4-fragmented; 1 not on an https veocdn.com link: "
     "cdn.example.test; 1 without a link"),
    ([{"mime_type": "https://c.veocdn.com/m1/standard/video-1080.mp4?Signature=SECRET", "url": "https://c.veocdn.com/m1/v.mp4"}],
     "VEO lists 1 " + NONE_OF_THEM + "1 of type unrecognised"),
    ([{"mime_type": "video/mp4", "url": f"https://h{i}.example.test/a.mp4?sig=SECRET", "height": 720} for i in range(5)],
     "VEO lists 5 " + NONE_OF_THEM + "5 not on an https veocdn.com link: h0.example.test, h1.example.test, "
     "h2.example.test and 2 more"),
])
def test_no_downloadable_file_fails_closed_and_says_what_was_left_out(net, rows, said):
    serve(net, rows=rows, meta=match_json(processing_status="processing", expiration_status="expired"))
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value) == said + " (processing_status=processing, expiration_status=expired)"
    assert "SECRET" not in str(e.value) and "/a.mp4" not in str(e.value)
    assert not net.to("veocdn.com") and not net.to("example.test")


@pytest.mark.parametrize("mime", ["video/mp4; codecs=avc1", "VIDEO/MP4", " video/mp4 ", "video/webm;codecs=vp9"])
def test_media_type_parameters_and_case_are_read_like_the_old_tool(monkeypatch, mime):
    row = {"mime_type": mime, "url": "https://c.veocdn.com/m1/standard/video-1080.mp4", "height": 1080,
           "width": 1920, "render_type": "standard", "bit_rate": 5000}
    assert veo.eligible_rows([row]) == [row]
    assert veo.pick_file([row], "1080p", True) is row
    plain = dict(row, mime_type=mime.partition(";")[0].strip().lower(), url=row["url"] + "?plain")
    assert veo.pick_file([plain, row], "best", True) is row        # same ext as the plain row: list order decides, as before
    formats = ytdlp_veo_formats(monkeypatch, [row], lambda url: True)
    assert [f["ext"] for f in formats] == [veo._MIME_EXT[veo._mime(row)]]


@pytest.mark.parametrize("webm", ["Video/WebM; codecs=vp9", "VIDEO/WEBM", "video/webm;codecs=vp8"])
@pytest.mark.parametrize("ffmpeg", [True, False])
def test_a_decorated_webm_type_still_counts_as_webm_in_the_pick(monkeypatch, webm, ffmpeg):
    mp4 = api_row("standard", 1080)
    other = dict(api_row("standard", 1080, "video/webm"), mime_type=webm)
    formats = ytdlp_veo_formats(monkeypatch, [mp4, other], lambda url: True)
    for quality in QUALITIES:
        want = ytdlp_pick_url(formats, fmt_for(quality, ffmpeg))
        assert want == mp4["url"]                      # yt-dlp prefers mp4 over webm at the same size
        assert veo.pick_file([mp4, other], quality, ffmpeg)["url"] == want


def decorate(rows, rng):
    """The same rows with media types written the ways VEO might: case, spaces, parameters."""
    forms = [lambda m: m, str.upper, str.title, lambda m: f" {m} ", lambda m: m + "; codecs=avc1",
             lambda m: m.upper() + ";"]
    return [dict(r, mime_type=rng.choice(forms)(r["mime_type"])) for r in rows]


def test_pick_equals_ytdlp_selector_with_decorated_media_types():
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.utils import mimetype2ext
    rng = random.Random(20261120)
    compared, bad = 0, []
    for rows in [decorate(r, rng) for r in list(SHAPES.values()) + random_sets()]:
        eligible = veo.eligible_rows(rows)
        formats = [dict(f, ext=mimetype2ext(r["mime_type"])) for f, r in zip(veo_formats(
            [dict(r, mime_type=veo._mime(r)) for r in eligible]), eligible)]
        for quality in QUALITIES:
            for ffmpeg in (True, False):
                want = ytdlp_pick_url(formats, fmt_for(quality, ffmpeg))
                got = veo.pick_file(rows, quality, ffmpeg)["url"]
                compared += 1
                if got != want:
                    bad.append((quality, ffmpeg, [r["mime_type"] for r in eligible], got, want))
    print(f"oracle yt-dlp {yt_dlp.version.__version__}, decorated types: compared {compared}, differing {len(bad)}")
    assert compared == 3696
    assert bad == []


def test_unreachable_api_is_an_extraction_error_without_the_url(net):
    net.routes[API_MATCH] = requests.exceptions.ConnectionError(f"HTTPSConnectionPool(host='app.veo.co'): {API_MATCH}")
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value) == "Could not reach VEO's match API: ConnectionError"
    assert len(net.calls) == 2    # one retry, on a connection error


def test_a_timeout_is_one_retry_then_an_extraction_error(net):
    net.routes[API_MATCH] = requests.exceptions.ReadTimeout("read timed out")
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value) == "Could not reach VEO's match API: ReadTimeout"
    assert len(net.calls) == 2
    assert all(c["kw"]["timeout"] == (15, 30) for c in net.calls)


def test_a_4xx_is_never_retried_and_a_5xx_once(net):
    net.routes[API_MATCH] = cloudfront_403()
    with pytest.raises(ExtractionError):
        extract(MATCH)
    assert len(net.calls) == 1
    net.calls.clear()
    net.routes[API_MATCH] = resp(500, b"oops", {"Server": "nginx"})
    with pytest.raises(ExtractionError) as e:
        extract(MATCH)
    assert str(e.value).startswith("VEO match API answered HTTP 500")
    assert len(net.calls) == 2
    net.calls.clear()
    net.routes[API_MATCH] = [resp(502, b"bad"), js(match_json())]
    net.routes[API_VIDEOS] = js([api_row("standard", 1080)])
    assert extract(MATCH).video_files
    assert len(net.to("/api/app/matches/%s" % SLUG)) == 3


@pytest.mark.parametrize("url", [
    "https://app.veo.co/clubs/abc/", "https://app.veo.co/", "https://veo.co/matches/abc",
    "https://app.veo.co.evil.test/matches/abc", "https://live.veo.co/watch/1"])
def test_other_veo_links_are_not_match_links(net, url):
    with pytest.raises(ExtractionError) as e:
        extract(url)
    assert "not a VEO match link" in str(e.value)
    assert net.calls == []


def test_a_cdn_link_is_one_file_downloaded_as_is(net, tmp_path):
    url = "https://c.veocdn.com/m1/standard/video.mp4"
    net.routes[url] = file_server()
    r = extract(url)
    assert [f["url"] for f in r.video_files] == [url]
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "done" and (tmp_path / "o.mp4").read_bytes() == DATA


def test_veo_ignores_session_token_and_cookies(net):
    serve(net)
    extract(MATCH, cookies={"a": "b"}, session_token="PLANTPW_9f3a")
    for c in net.calls:
        assert "PLANTPW_9f3a" not in json.dumps(c["headers"]) and "Cookie" not in c["headers"]


# ── STEP 3: which file ──────────────────────────────────────────────────────

FFMPEG_FMT = {
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best/b",
    "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best/b",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best/b",
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best/b",
}
NO_FFMPEG_FMT = {
    "720p": "best[height<=720][ext=mp4]/best[height<=720]/best",
    "480p": "best[height<=480][ext=mp4]/best[height<=480]/best",
    "1080p": "best[height<=720][ext=mp4]/best[height<=720]/best",
    "best": "best[ext=mp4]/best",
}
QUALITIES = ["1080p", "720p", "480p", "best", "worst", "4k"]


def fmt_for(quality, ffmpeg):
    return (FFMPEG_FMT if ffmpeg else NO_FFMPEG_FMT)[quality if quality in ("1080p", "720p", "480p") else "best"]


def with_ffmpeg(monkeypatch, present):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "ffmpeg" if present else None)


def veo_formats(rows):
    """The format list yt-dlp's Veo extractor builds from the rows it keeps (same fields, same order)."""
    ext = {"video/mp4": "mp4", "video/webm": "webm", "image/jpeg": "jpg"}
    out = []
    for r in rows:
        if r["mime_type"] == "video/mp2t":
            continue
        h = r.get("height")
        out.append({"url": r["url"], "format_id": f"{r['render_type']}-{h}p" if r.get("render_type") and h else None,
                    "ext": ext[r["mime_type"]], "width": r.get("width"), "height": h,
                    "vbr": (r["bit_rate"] // 1000) if r.get("bit_rate") is not None else None})
    return out


SHAPES = {
    "standard-first": [api_row("standard", 1080), api_row("standard", 480), api_row("panorama", 2048)],
    "panorama-first": [api_row("panorama", 2048), api_row("standard", 1080), api_row("standard", 480)],
    "standard-only": [api_row("standard", 1080), api_row("standard", 480)],
    "panorama-only": [api_row("panorama", 2048)],
    "mixed-heights": [api_row("standard", 720), api_row("panorama", 1440), api_row("standard", 1080),
                      api_row("standard", 360), api_row("panorama", 2048), api_row("standard", 480)],
    "nothing-under-480": [api_row("standard", 1080)],
    "thumbnail-and-config": [api_row("standard", 480, "image/jpeg"), api_row("panorama", 2048, "video/mp2t"),
                             api_row("standard", 1080), api_row("standard", 480)],
    "mp4-and-webm": [api_row("standard", 1080, "video/webm"), api_row("standard", 1080), api_row("standard", 720, "video/webm")],
}


def random_sets(count=300, seed=20261119):
    rng = random.Random(seed)
    out = []
    for i in range(count):
        rows = []
        for j in range(rng.randint(1, 6)):
            rt, h, mime = rng.choice(["standard", "panorama"]), rng.choice([360, 480, 720, 1080, 1440, 2048]), \
                rng.choice(["video/mp4", "video/webm"])
            rows.append({"mime_type": mime, "url": f"https://c.veocdn.com/m{i}/{rt}/r{j}-{h}.{mime.split('/')[1]}",
                         "height": h, "width": rng.choice([int(h * 16 / 9), None]), "render_type": rt,
                         "bit_rate": rng.choice([None, 0, 999, 1500, 5000, 2500000, 5000])})
        out.append(rows)
    return out


_YDL = {}


def ytdlp_pick_url(formats, fmt):
    """The file yt-dlp's own selector chooses for this -f from this format list (its own sorting included)."""
    yt_dlp = pytest.importorskip("yt_dlp")
    if fmt not in _YDL:
        _YDL[fmt] = yt_dlp.YoutubeDL({"format": fmt, "quiet": True, "no_warnings": True, "simulate": True})
    info = {"id": "m", "title": "Match", "extractor": "Veo", "extractor_key": "Veo", "webpage_url": MATCH,
            "formats": copy.deepcopy(formats)}
    return _YDL[fmt].process_ie_result(info, download=False)["url"]


def test_pick_equals_ytdlp_selector(monkeypatch):
    yt_dlp = pytest.importorskip("yt_dlp")
    sets = list(SHAPES.values()) + random_sets()
    compared, bad = 0, []
    for rows in sets:
        eligible = veo.eligible_rows(rows)
        formats = veo_formats(eligible)
        for quality in QUALITIES:
            for ffmpeg in (True, False):
                want = ytdlp_pick_url(formats, fmt_for(quality, ffmpeg))
                got = veo.pick_file(rows, quality, ffmpeg)["url"]
                compared += 1
                if got != want:
                    bad.append((quality, ffmpeg, [(r["render_type"], r["height"], r["mime_type"][6:], r["bit_rate"]) for r in eligible],
                                got, want))
    print(f"oracle yt-dlp {yt_dlp.version.__version__}: compared {compared}, differing {len(bad)}")
    assert compared == 3696
    assert bad == []


def test_pick_file_returns_a_row_of_the_list_it_was_given():
    rows = SHAPES["standard-first"]
    assert veo.pick_file(rows, "1080p", True) is rows[0]
    assert veo.pick_file(rows, "480p", True) is rows[1]
    assert veo.pick_file(rows, "best", True) is rows[2]
    assert veo.pick_file(rows, "1080p", False) is rows[1]    # without ffmpeg 1080p becomes <=720: the old quirk


JUNK = [
    {"mime_type": "image/jpeg", "url": "https://c.veocdn.com/m1/thumb-4000.jpg", "height": 4000, "render_type": "standard"},
    {"mime_type": "video/mp2t", "url": "https://c.veocdn.com/m1/pano.m3u8", "height": 4000, "render_type": "panorama"},
    {"mime_type": "video/mp4", "url": "https://cdn.example.test/other-2160.mp4", "height": 2160, "render_type": "standard"},
    {"mime_type": "video/mp4", "url": "http://c.veocdn.com/m1/plain-2160.mp4", "height": 2160, "render_type": "standard"},
    {"mime_type": "video/mp4", "height": 2160, "render_type": "standard"},
    {"mime_type": "video/mp4", "url": "https://c.veocdn.com.evil.test/m1/x.mp4", "height": 2160, "render_type": "standard"},
    "not a row", None,
]


@pytest.mark.parametrize("quality", QUALITIES)
@pytest.mark.parametrize("ffmpeg", [True, False])
def test_junk_rows_are_never_picked(quality, ffmpeg):
    assert veo.pick_file(JUNK, quality, ffmpeg) is None
    good = api_row("standard", 480)
    assert veo.pick_file(JUNK + [good], quality, ffmpeg) is good
    assert veo.pick_file([good] + JUNK, quality, ffmpeg) is good


def ytdlp_veo_formats(monkeypatch, rows, valid):
    """The format list yt-dlp's own Veo extractor builds for these rows, with its own panorama check answered by `valid`."""
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.extractor.veo import VeoIE
    meta = {"title": "t"}
    monkeypatch.setattr(VeoIE, "_download_json", lambda self, url, vid, *a, **k: rows if url.endswith("/videos") else meta)
    monkeypatch.setattr(VeoIE, "_is_valid_url", lambda self, url, vid, item="video", headers={}: valid(url))
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return VeoIE(ydl)._real_extract(MATCH)["formats"]


@pytest.mark.parametrize("shape", ["standard-first", "panorama-first", "panorama-only", "mixed-heights",
                                   "thumbnail-and-config", "standard-only"])
@pytest.mark.parametrize("pano_status", [404, 403, 200])
@pytest.mark.parametrize("quality", QUALITIES)
@pytest.mark.parametrize("ffmpeg", [True, False])
def test_invalid_panorama_is_skipped_like_ytdlp(monkeypatch, net, shape, pano_status, quality, ffmpeg):
    rows = SHAPES[shape]
    pano_urls = {r["url"] for r in rows if r.get("render_type") == "panorama" and r["mime_type"] != "video/mp2t"}
    formats = ytdlp_veo_formats(monkeypatch, rows, lambda url: not (url in pano_urls and pano_status >= 400))
    for r in rows:
        net.routes[r["url"]] = resp(pano_status if r["url"] in pano_urls else 200, b"x",
                                    {"Content-Type": "video/mp4"})
    ours = {r["url"] for r in veo.eligible_rows(rows)}
    kept = [f for f in formats if f["url"] in ours]     # yt-dlp also keeps image rows; this importer never does
    want = ytdlp_pick_url(kept, fmt_for(quality, ffmpeg)) if kept else None
    got = veo.choose_file(rows, quality, ffmpeg)
    assert (got["url"] if got else None) == want
    asked = [c["url"] for c in net.to("veocdn.com")]
    assert len(asked) == len(set(asked)) and set(asked) <= pano_urls    # only panorama files are asked for, once each
    assert all(c["method"] == "GET" for c in net.to("veocdn.com"))


def test_an_invalid_picked_panorama_costs_exactly_one_request(net):
    rows = [api_row("standard", 1080), api_row("standard", 480), api_row("panorama", 2048)]
    for r in rows:
        net.routes[r["url"]] = resp(404 if r["render_type"] == "panorama" else 200, b"x")
    assert veo.choose_file(rows, "best", True) is rows[0]
    assert len(net.to("veocdn.com")) == 1
    net.calls.clear()
    assert veo.choose_file(rows, "1080p", True) is rows[0]      # a panorama is not the pick: not asked at all
    assert net.calls == []


# ── STEP 4: the download ────────────────────────────────────────────────────

def one_file(net, route, rows=None):
    rows = serve(net, rows=rows, file_route=route)
    return extract(MATCH), rows


def test_download_writes_the_file_and_logs_no_url(net, tmp_path, caplog):
    caplog.set_level("INFO")
    r, _ = one_file(net, file_server())
    seen = []
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p", progress_callback=lambda pr: seen.append(pr.percent))
    assert p.status == "done" and p.percent == 100.0 and (tmp_path / "o.mp4").read_bytes() == DATA
    assert not (tmp_path / "o.mp4.part").exists()
    assert seen and seen[-1] == 100.0
    lines = [m for m in caplog.messages if m.startswith("VEO:")]
    assert lines == ["VEO: picked standard-1080p 1920x1080 from 1 file(s), downloading from c.veocdn.com",
                     f"VEO: downloaded {SIZE} of {SIZE} bytes"]
    assert "/m1/" not in " ".join(caplog.messages) and "Signature" not in " ".join(caplog.messages)


def test_download_resumes_after_a_dropped_connection(net, tmp_path):
    r, _ = one_file(net, file_server(drop_first_at=20000))
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "done", p.error
    assert (tmp_path / "o.mp4").read_bytes() == DATA
    ranges = [c["headers"].get("Range") for c in net.to("c.veocdn.com")]
    assert ranges == [None, "bytes=20000-"]
    assert not (tmp_path / "o.mp4.part").exists()


def test_download_restarts_when_range_is_ignored(net, tmp_path):
    def route(request, n):
        if n == 1:
            return resp(200, DATA, {"Server": "AmazonS3"}, fault_after=20000)
        return resp(200, DATA, {"Server": "AmazonS3"})      # a Range request answered 200: the whole file again
    r, _ = one_file(net, route)
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "done", p.error
    assert (tmp_path / "o.mp4").read_bytes() == DATA
    assert [c["headers"].get("Range") for c in net.to("c.veocdn.com")] == [None, "bytes=20000-"]


def test_download_fails_closed_without_a_size(net, tmp_path):
    r, _ = one_file(net, file_server(no_length=True))
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "error" and "did not say how large the file is" in p.error
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.mp4.part").exists()
    assert len(net.to("c.veocdn.com")) == 1


def test_download_fails_on_a_short_file_and_leaves_no_part_file(net, tmp_path):
    r, _ = one_file(net, file_server(short_to=40000))
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "error"
    assert "of 60000 bytes" in p.error
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.mp4.part").exists()
    assert len(net.to("c.veocdn.com")) == 5      # 5 attempts, no more


def test_download_gives_up_after_five_attempts_with_growing_pauses(net, tmp_path, monkeypatch):
    pauses = []
    monkeypatch.setattr(time, "sleep", lambda s: pauses.append(s))
    r, _ = one_file(net, lambda request, n: requests.exceptions.ConnectionError("reset"))
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "error" and "could not reach c.veocdn.com: ConnectionError" in p.error
    assert len(net.to("c.veocdn.com")) == 5 and pauses == [2, 4, 8, 16]


@pytest.mark.parametrize("code", [403, 404, 416])
def test_cdn_refusal_names_the_host_only(net, tmp_path, code):
    rows = [dict(api_row("standard", 1080), url=SIGNED_1080)]
    body = b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'
    r, _ = one_file(net, resp(code, body, {"Server": "AmazonS3"}), rows=rows)
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "error"
    assert p.error == f"c.veocdn.com answered HTTP {code}"
    assert "Signature" not in p.error and "/m1/" not in p.error and "PLANT" not in p.error
    assert len(net.to("c.veocdn.com")) == 1      # a 4xx is not retried
    assert not (tmp_path / "o.mp4.part").exists()


def test_a_5xx_from_the_cdn_is_retried(net, tmp_path):
    r, _ = one_file(net, lambda request, n: resp(503, b"slow down") if n == 1 else file_server()(request, n))
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "done" and (tmp_path / "o.mp4").read_bytes() == DATA


def test_a_resume_that_does_not_continue_the_file_fails(net, tmp_path):
    def route(request, n):
        if n == 1:
            return resp(200, DATA, {"Server": "AmazonS3"}, fault_after=20000)
        return resp(206, DATA[5:], {"Content-Range": f"bytes 5-{SIZE - 1}/{SIZE}"})
    r, _ = one_file(net, route)
    p = veo.download_file(r, str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "error" and "does not continue the file" in p.error
    assert not (tmp_path / "o.mp4.part").exists()


def test_download_picks_the_file_the_quality_names(net, tmp_path, monkeypatch):
    with_ffmpeg(monkeypatch, True)
    rows = [api_row("standard", 1080), api_row("standard", 480)]
    r, _ = one_file(net, file_server(), rows=rows)
    assert veo.download_file(r, str(tmp_path / "a.mp4"), "480p").status == "done"
    assert [c["url"] for c in net.to("c.veocdn.com")] == [rows[1]["url"]]


def test_a_result_with_no_files_ends_in_error_without_raising(net, tmp_path):
    p = veo.download_file(ExtractResult(title="t", platform="veo", video_files=[]), str(tmp_path / "o.mp4"), "1080p")
    assert p.status == "error" and "no downloadable video file" in p.error


# ── the self-check ──────────────────────────────────────────────────────────

def test_self_check_ok_reads_one_kib_with_a_range(net, capsys):
    serve(net, file_route=file_server())
    assert veo.check(MATCH) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "RESULT OK" and len(out) <= 8
    cdn = net.to("c.veocdn.com")
    assert len(net.to("app.veo.co")) == 2 and len(cdn) == 1 and cdn[0]["headers"]["Range"] == "bytes=0-1023"
    assert not [l for l in out if "/m1/" in l or "Signature" in l]


def test_self_check_names_the_refusal(net, capsys):
    net.routes[API_MATCH] = cloudfront_403()
    assert veo.check(MATCH) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "RESULT FAIL match-api 403" and any("HTTP 403" in l for l in out)


@pytest.mark.parametrize("meta,said,result", [
    ({"privacy": "public", "is_accessible": False},
     "VEO marks this match as not accessible (privacy=public, is_accessible=False).", "RESULT FAIL match-api not-accessible"),
    ({"privacy": None, "is_accessible": False},
     "VEO marks this match as not accessible (privacy=None, is_accessible=False).", "RESULT FAIL match-api not-accessible"),
    ({"privacy": "private", "is_accessible": False},
     "VEO requires authentication. / VEO says this match is not public (privacy=private, is_accessible=False).",
     "RESULT FAIL match-api private"),
])
def test_self_check_prints_the_whole_reason_and_its_own_result(net, capsys, meta, said, result):
    serve(net, meta=match_json(**meta))
    assert veo.check(MATCH) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == result
    assert any(l.startswith(said) for l in out), out


def test_self_check_does_not_cut_the_left_out_list(net, capsys):
    rows = [{"mime_type": "video/x-mp4-fragmented", "url": "https://c.veocdn.com/m1/v.mp4", "height": 1080},
            {"mime_type": "video/mp4", "url": "https://cdn.example.test/a.mp4?sig=SECRET", "height": 720}, "junk"]
    serve(net, rows=rows, meta=match_json(processing_status="processing", expiration_status="expired"))
    assert veo.check(MATCH) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "RESULT FAIL video-list 200"
    assert any(l.endswith("1 without a link (processing_status=processing, expiration_status=expired)") for l in out), out
    assert not [l for l in out if "SECRET" in l or "/a.mp4" in l]


# ── the other platforms are untouched ───────────────────────────────────────

class Ytdlp:
    """Stands in for `python -m yt_dlp`. Records every command; runs nothing."""

    def __init__(self, info=None):
        self.info = info
        self.info_cmds = []
        self.download_cmds = []
        self.rc = 0

    def run(self, cmd, *a, **k):
        assert list(cmd[:3]) == PY_YTDLP, f"unexpected command: {list(cmd)[:3]}"
        self.info_cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(self.info), stderr="")

    def popen(self, cmd, *a, **k):
        assert list(cmd[:3]) == PY_YTDLP, f"unexpected command: {list(cmd)[:3]}"
        self.download_cmds.append(list(cmd))
        with open(cmd[cmd.index("-o") + 1], "wb") as f:
            f.write(b"\0" * 4096)
        proc = type("Proc", (), {})()
        proc.stdout = iter(["[download]  50.0% of 1.00GiB at 5.00MiB/s ETA 00:10",
                            "[download] 100% of 1.00GiB"])
        proc.returncode = self.rc
        proc.wait = lambda: self.rc
        return proc


def fake_ytdlp(monkeypatch, **kw):
    y = Ytdlp(**kw)
    monkeypatch.setattr(subprocess, "run", y.run)
    monkeypatch.setattr(subprocess, "Popen", y.popen)
    return y


def header_values(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-header"]


def api_job(monkeypatch, tmp_path, url, platform):
    import api_service as api
    monkeypatch.setattr(api, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(api, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(api, "NORMALIZE_VIDEO", False)
    monkeypatch.setattr(api, "S3_BUCKET", "")
    monkeypatch.setattr(api, "SPORTSREELZ_UPLOAD_URL", "")
    job = {"job_id": "6662f930", "video_url": url, "platform": platform, "user_id": "u1",
           "quality": "1080p", "title": None, "status": "queued", "percent": 0.0,
           "created_at": 0, "updated_at": 0}
    monkeypatch.setitem(api.jobs, "6662f930", job)
    return api, job


@pytest.mark.parametrize("quality", QUALITIES)
@pytest.mark.parametrize("ffmpeg", [True, False])
def test_the_format_selector_is_the_one_the_page_url_download_used(monkeypatch, tmp_path, quality, ffmpeg):
    # Pins the -f strings the picker oracle above uses to what multi_dl really builds (fed a non-VEO result).
    with_ffmpeg(monkeypatch, ffmpeg)
    y = fake_ytdlp(monkeypatch, info={})
    r = ExtractResult(title="t", platform="hudl", use_ytdlp=True, source_url="https://fan.hudl.com/watch?b=1")
    p = multi_dl.download_with_ytdlp(r, str(tmp_path / "out.mp4"), quality)
    assert p.status == "done"
    cmd = y.download_cmds[0]
    assert cmd[cmd.index("-f") + 1] == fmt_for(quality, ffmpeg)


def test_youtube_still_downloads_by_page_url_with_its_own_arguments(monkeypatch, tmp_path):
    y = fake_ytdlp(monkeypatch, info={"title": "Some video", "duration": 60})
    r = extract(YT)
    assert r.platform == "youtube"
    assert r.source_url == YT and r.direct_url is None
    assert r.headers["Referer"] == "https://www.youtube.com/"
    assert "Chrome/133.0.0.0" in r.headers["User-Agent"]
    p = multi_dl.download_with_ytdlp(r, str(tmp_path / "out.mp4"), "1080p")
    assert p.status == "done"
    cmd = y.download_cmds[0]
    assert cmd[-1] == YT
    assert "--extractor-args" in cmd
    assert any(a.startswith("youtube:player_client=") for a in cmd)
    assert "Referer:https://www.youtube.com/" in header_values(cmd)


def test_yt_dlp_result_with_a_page_url_and_a_file_url_still_downloads_the_page_url(monkeypatch, tmp_path):
    # HUDL's direct-stream branch (extractors/hudl.py _parse_hudl_streams) builds
    # this shape; multi_dl's rule "source_url wins when present" is unchanged.
    y = fake_ytdlp(monkeypatch, info={})
    r = ExtractResult(title="t", platform="hudl", direct_url="https://vcloud.hudl.com/x.mp4",
                      use_ytdlp=True, source_url="https://fan.hudl.com/watch?b=1",
                      headers={"X-Test": "1"})
    p = multi_dl.download_with_ytdlp(r, str(tmp_path / "out.mp4"), "1080p")
    assert p.status == "done"
    cmd = y.download_cmds[0]
    assert cmd[-1] == "https://fan.hudl.com/watch?b=1"
    assert header_values(cmd) == ["X-Test:1"]


@pytest.mark.parametrize("platform,url", [
    ("hudl", "https://fan.hudl.com/usa/nc/x/watch?b=1"),
    ("trace", "https://app.traceup.com/watch/1"),
    ("pixellot", "https://pixellot.tv/watch/1"),
])
def test_stream_platforms_still_use_their_own_downloader_not_yt_dlp(monkeypatch, tmp_path, platform, url):
    y = fake_ytdlp(monkeypatch, info={})
    api, job = api_job(monkeypatch, tmp_path, url, platform)
    m3u8 = "https://cdn.example.test/master.m3u8"
    seen = {}
    monkeypatch.setattr(api, "extract", lambda u, cookies=None, session_token=None: ExtractResult(
        title="t", platform=platform, m3u8_url=m3u8, headers={"Referer": "https://r.example.test/"}))
    monkeypatch.setattr(api, "fetch_and_select", lambda u, h, b, q: (u, None, None))

    class FakeDl:
        def __init__(self, ffmpeg_path):
            pass

        def download(self, u, out, headers, cb):
            seen["url"], seen["headers"] = u, headers
            with open(out, "wb") as f:
                f.write(b"\0" * 4096)
            p = DownloadProgress()
            p.status = "done"
            return p

    monkeypatch.setattr(api, "HudlDownloader", FakeDl)
    api._process_job("6662f930")
    assert job["status"] == "done", job.get("error")
    assert seen == {"url": m3u8, "headers": {"Referer": "https://r.example.test/"}}
    assert y.info_cmds == [] and y.download_cmds == []


# ── STEP 5: the wiring ──────────────────────────────────────────────────────

def test_whole_job_done_two_api_calls_no_ytdlp(monkeypatch, net, tmp_path):
    y = fake_ytdlp(monkeypatch, info={})
    serve(net, file_route=file_server())
    api, job = api_job(monkeypatch, tmp_path, MATCH, "veo")
    api._process_job("6662f930")
    assert job["status"] == "done", job.get("error")
    assert job["title"] == "Match - LPFC N1 G2008_09 U19" or job["title"].startswith("Match - LPFC N1")
    assert len(net.to("app.veo.co")) == 2
    assert y.info_cmds == [] and y.download_cmds == []
    assert not any("Authorization" in c["headers"] for c in net.calls)
    assert (tmp_path / "6662f930").is_dir()
    assert [p.read_bytes() for p in (tmp_path / "6662f930").glob("*.mp4")] == [DATA]


def test_hudl_direct_url_with_use_ytdlp_still_goes_to_ytdlp(monkeypatch, tmp_path):
    y = fake_ytdlp(monkeypatch, info={})
    api, job = api_job(monkeypatch, tmp_path, "https://fan.hudl.com/usa/nc/x/watch?b=1", "hudl")
    monkeypatch.setattr(api, "extract", lambda u, cookies=None, session_token=None: ExtractResult(
        title="t", platform="hudl", direct_url="https://vcloud.hudl.com/x.mp4", use_ytdlp=True,
        source_url="https://fan.hudl.com/watch?b=1"))
    api._process_job("6662f930")
    assert job["status"] == "done", job.get("error")
    assert len(y.download_cmds) == 1 and y.download_cmds[0][-1] == "https://fan.hudl.com/watch?b=1"


def test_cli_download_single_veo_uses_the_plain_download(monkeypatch, net, tmp_path):
    y = fake_ytdlp(monkeypatch, info={})
    serve(net, file_route=file_server())
    ok = multi_dl.download_single(MATCH, str(tmp_path), "1080p", ffmpeg_path="ffmpeg")
    assert ok is True
    assert y.info_cmds == [] and y.download_cmds == []
    assert [p.read_bytes() for p in tmp_path.glob("*.mp4")] == [DATA]


# ── STEP 6: the password never leaves the request ───────────────────────────

PLANT = "PLANTPW_9f3a"


def test_platform_password_never_leaves_the_request(monkeypatch, net, tmp_path, caplog):
    from concurrent.futures import ThreadPoolExecutor
    y = fake_ytdlp(monkeypatch, info={})
    serve(net, file_route=file_server())
    api, _ = api_job(monkeypatch, tmp_path, MATCH, "veo")
    api.jobs.clear()
    ex = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(api, "executor", ex)
    caplog.set_level("DEBUG")
    req = api.ImportRequest(user_id="u1", video_url=MATCH, platform="veo", platform_email="plant@example.invalid",
                            platform_password=PLANT, quality="1080p")
    try:
        api.start_import(req).send(None)      # no await inside, and an event loop needs sockets this file forbids
    except StopIteration as done:
        resp_ = done.value
    ex.shutdown(wait=True)
    job = api.jobs[resp_.job_id]
    assert job["status"] == "done", job.get("error")
    api._persist_jobs()
    saved = (tmp_path / "jobs.json").read_bytes().decode("utf-8", "replace")
    outgoing = json.dumps([[c["url"], c["headers"]] for c in net.calls])
    argvs = json.dumps(y.info_cmds + y.download_cmds)
    for where, text in [("headers", outgoing), ("argv", argvs), ("jobs.json", saved), ("log", " ".join(caplog.messages)),
                        ("job", json.dumps(job)), ("paths", " ".join(str(p) for p in tmp_path.rglob("*")))]:
        assert PLANT not in text and "plant@example" not in text, where
    assert not any("Authorization" in c["headers"] for c in net.calls)
