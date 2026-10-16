"""HUDL cutup links (numeric ?v=): per-play MP4 clips, no HLS manifest.

Offline: HUDL is stubbed, clips are generated locally with ffmpeg and served
from a local HTTP server. Run: python -m pytest tests -q
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extractors import hudl as hudl_mod  # noqa: E402
from extractors.base import AuthRequiredError, ExtractionError  # noqa: E402
from downloader import download_clips  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")

CUTUP_URL = "https://app.hudl.com/watch/team/14016/analyze?v=96200833&team=14016&cx=tm&l=vdpm0"
B64_URL = "https://app.hudl.com/watch/team/720870/analyze?v=VmlkZW82OGRmMzU1NjUyMzljNjVlMGY5ZTE2ZTk%3D&team=720870"


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def clip(n, ticks=134666670, files=None):
    files = files if files is not None else [
        {"quality": 25, "fileName": f"https://vg.hudl.com/x/c{n}_480_1100.mp4?v=1"},
        {"quality": 75, "fileName": f"https://vg.hudl.com/x/c{n}_1080_3000.mp4?v=1"},
    ]
    return {"clipId": n, "angles": [{"angleName": "Wide", "files": files, "duration": ticks}],
            "tags": [{"key": "PLAY #", "values": [str(n)]}]}


class FakeSession:
    """Stands in for requests.Session inside _extract_app_hudl."""

    def __init__(self, clips_resp, cutups_resp=None, video_resp=None):
        self.cookies = _Jar()
        self.clips_resp = clips_resp
        self.cutups_resp = cutups_resp or FakeResp(200, [{"name": "LHS vs. Strasburg High Schoo 08/15/2026"}])
        self.video_resp = video_resp
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        assert url.endswith("/clips"), url
        return self.clips_resp

    def put(self, url, json=None, **kw):
        self.calls.append(("PUT", url, json))
        return self.cutups_resp

    def post(self, url, json=None, **kw):
        q = (json or {}).get("query", "")
        self.calls.append(("POST", url, q[:20]))
        if "team(id" in q:
            return FakeResp(200, {"data": {"team": {"name": "Boys Varsity Football",
                                                    "school": {"fullName": "Strasburg High School"}}}})
        if self.video_resp is not None:
            return self.video_resp
        return FakeResp(200, {"data": {"video": None}})


class _Jar:
    def set(self, *a, **kw):
        pass


def run_extract(monkeypatch, session, url=CUTUP_URL):
    import requests
    monkeypatch.setattr(requests, "Session", lambda: session)
    return hudl_mod.HudlExtractor().extract(url, cookies={"ident": "x"})


def test_numeric_v_returns_ordered_best_quality_clips(monkeypatch):
    s = FakeSession(FakeResp(200, {"clipsWithTags": [clip(1), clip(2, ticks=64666670), clip(3)]}))
    r = run_extract(monkeypatch, s)
    assert r.clip_urls == [f"https://vg.hudl.com/x/c{n}_1080_3000.mp4?v=1" for n in (1, 2, 3)]
    assert r.m3u8_url is None and not r.use_ytdlp
    assert r.title == "LHS vs. Strasburg High Schoo 08/15/2026"
    assert abs(r.duration - (13.466667 * 2 + 6.466667)) < 0.01
    assert set(r.headers) == {"User-Agent"}
    assert r.team_name == "Strasburg High School - Boys Varsity Football"
    assert ("PUT", "https://www.hudl.com/api/v2/classicfacade/team/14016/cutups/getCutups",
            {"cutupIds": ["96200833"]}) in s.calls
    assert not any(c[0] == "POST" and "query GetVideo" in c[2] for c in s.calls)


def test_cutup_title_falls_back_when_name_lookup_fails(monkeypatch):
    s = FakeSession(FakeResp(200, {"clipsWithTags": [clip(1)]}), cutups_resp=FakeResp(405, None))
    assert run_extract(monkeypatch, s).title == "hudl_video_96200833"


def test_cutup_404_is_reported_as_hudl_side(monkeypatch):
    s = FakeSession(FakeResp(404, {"Message": "NotFound"}))
    with pytest.raises(ExtractionError, match="clip list returns NotFound"):
        run_extract(monkeypatch, s)


@pytest.mark.parametrize("status", [401, 403])
def test_cutup_no_access_is_auth_error(monkeypatch, status):
    with pytest.raises(AuthRequiredError):
        run_extract(monkeypatch, FakeSession(FakeResp(status, {})))


def test_cutup_play_without_file_fails_loudly(monkeypatch):
    s = FakeSession(FakeResp(200, {"clipsWithTags": [clip(1), clip(2, files=[]), clip(3)]}))
    with pytest.raises(ExtractionError, match="1 of 3 plays have no video file"):
        run_extract(monkeypatch, s)


def test_cutup_empty_and_bad_json(monkeypatch):
    with pytest.raises(ExtractionError, match="has no clips"):
        run_extract(monkeypatch, FakeSession(FakeResp(200, {"clipsWithTags": []})))
    with pytest.raises(ExtractionError, match="was not JSON"):
        run_extract(monkeypatch, FakeSession(FakeResp(200, ValueError("x"))))


def test_base64_v_keeps_the_graphql_stream_path(monkeypatch):
    m3u8 = "https://vh.hudl.com/abc/master.m3u8"
    s = FakeSession(clips_resp=None,
                    video_resp=FakeResp(200, {"data": {"video": {"title": "Game", "playbackUrl": m3u8}}}))
    r = run_extract(monkeypatch, s, url=B64_URL)
    assert r.m3u8_url == m3u8 and r.clip_urls == []
    assert not any(c[0] in ("GET", "PUT") for c in s.calls)


# ── download_clips: real ffmpeg, local HTTP ─────────────────────────────

def make_clip(path, seconds, audio, timescale=None):
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"testsrc=size=320x180:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if timescale:
        cmd += ["-video_track_timescale", str(timescale)]
    subprocess.run(cmd + [str(path)], check=True)


def probe(path):
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries",
         "stream=codec_type:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    return float(d["format"]["duration"]), sorted(s["codec_type"] for s in d["streams"])


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@needs_ffmpeg
@pytest.mark.parametrize("audio", [False, True])
def test_download_clips_joins_in_order(tmp_path, served, audio):
    root, base = served
    lengths = [1, 2, 1]
    for i, sec in enumerate(lengths):
        make_clip(root / f"c{i}.mp4", sec, audio)
    out = tmp_path / "job" / "game.mp4"
    out.parent.mkdir()
    updates = []
    p = download_clips([f"{base}/c{i}.mp4" for i in range(3)], str(out),
                       {"User-Agent": "t"}, updates.append, ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    dur, kinds = probe(out)
    assert abs(dur - sum(lengths)) < 0.15
    assert kinds == ["audio", "video"]
    assert not (out.parent / ".clip_parts").exists()
    assert updates[-1].status == "done" and updates[-1].percent == 100.0


def color_clip(path, color, seconds):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c={color}:size=320x180:rate=30:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def pixel_at(path, seconds):
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(seconds), "-i", str(path),
                          "-vframes", "1", "-vf", "scale=1:1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(raw[:3])


@needs_ffmpeg
def test_download_clips_order_follows_the_list_not_finish_time(tmp_path, served):
    root, base = served
    color_clip(root / "red_long.mp4", "red", 3)
    color_clip(root / "blue_short.mp4", "blue", 1)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/red_long.mp4", f"{base}/blue_short.mp4"], str(out),
                       ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    r, g, b = pixel_at(out, 0.5)
    assert r > 150 and b < 100, (r, g, b)
    r, g, b = pixel_at(out, 3.5)
    assert b > 150 and r < 100, (r, g, b)
    assert abs(probe(out)[0] - 4) < 0.15


@needs_ffmpeg
def test_download_clips_missing_clip_fails_and_cleans_up(tmp_path, served):
    root, base = served
    make_clip(root / "c0.mp4", 1, False)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/c0.mp4", f"{base}/gone.mp4"], str(out),
                       ffmpeg_path=FFMPEG, retries=1)
    assert p.status == "error"
    assert "clip 2 of 2" in p.error
    assert not out.exists()
    assert not (out.parent / ".clip_parts").exists()


@needs_ffmpeg
def test_download_clips_mixed_sound_and_silent_clips_keep_full_length(tmp_path, served):
    # Live cutup 97367952: 59 clips with sound, 46 without; joining them as-is
    # dropped 94 s. Every clip must survive whatever the first one looks like.
    root, base = served
    layout = [(2, True), (1, False), (2, True), (1, False), (1, False), (2, True)]
    for i, (sec, audio) in enumerate(layout):
        make_clip(root / f"m{i}.mp4", sec, audio)
    for first_has_sound in (True, False):
        order = list(range(len(layout))) if first_has_sound else [1, 0, 2, 3, 4, 5]
        out = tmp_path / f"mixed_{first_has_sound}" / "game.mp4"
        out.parent.mkdir()
        p = download_clips([f"{base}/m{i}.mp4" for i in order], str(out), ffmpeg_path=FFMPEG)
        assert p.status == "done", p.error
        dur, kinds = probe(out)
        assert abs(dur - sum(s for s, _ in layout)) < 0.3, dur
        assert kinds == ["audio", "video"]


@needs_ffmpeg
def test_download_clips_title_with_quote(tmp_path, served):
    root, base = served
    make_clip(root / "q0.mp4", 1, False)
    make_clip(root / "q1.mp4", 1, True)
    out = tmp_path / "St. Mary's vs O'Neill.mp4"
    p = download_clips([f"{base}/q0.mp4", f"{base}/q1.mp4"], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    assert abs(probe(out)[0] - 2) < 0.2


@needs_ffmpeg
def test_download_clips_refuses_a_short_join(tmp_path, served, monkeypatch):
    import downloader
    root, base = served
    for i in range(3):
        make_clip(root / f"s{i}.mp4", 2, False)
    real = downloader._media_stats

    def lying(ffmpeg_path, path):
        frames, audio, secs = real(ffmpeg_path, path)
        return (frames // 2 if path.endswith("game.mp4") else frames), audio, secs

    monkeypatch.setattr(downloader, "_media_stats", lying)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/s{i}.mp4" for i in range(3)], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "error" and "refusing to ship an incomplete game" in p.error
    assert not out.exists()


def test_download_clips_empty_list(tmp_path):
    p = download_clips([], str(tmp_path / "g.mp4"), ffmpeg_path=FFMPEG or "ffmpeg")
    assert p.status == "error" and "no clips" in p.error


# ── opponent side ───────────────────────────────────────────────────────

@pytest.mark.parametrize("owner,team,opp,expected", [
    ("Strasburg High School - Boys Varsity Football", "LHS", "Strasburg High Schoo", "LHS"),
    ("Strasburg High School - Boys Varsity Football", "WHS", "Strasburg JV", "WHS"),
    ("Strasburg High School - Boys Varsity Football", "LHS", "Strasburg Drone", "LHS"),
    ("Strasburg High School - Boys Varsity Football", "SHS", "Wiggins (Drone)", "Wiggins (Drone)"),
    ("Strasburg High School - Boys Varsity Football", "Strasburg", "Limon", "Limon"),
    ("Ardrey Kell High School - Girls Varsity Soccer", "Ardrey Kell", "Myers Park", "Myers Park"),
    ("The High School", "A", "B High", "B High"),
    ("North Hills High School", "NHHS", "South Hills", "South Hills"),
    ("Central High School", "Lincoln-Way East", "Lincoln-Way Central", "Lincoln-Way Central"),
    ("Pleasant Hill High School - Girls Varsity Soccer", "Knob Noster girls soccer", "Pleasant Hill",
     "Knob Noster girls soccer"),
    ("Strasburg High School", "Strasburg", "Strasburg JV", "Strasburg JV"),
    ("North Hills High School", "Central", "North Hills", "Central"),
    ("St. Mary's Academy", "LHS", "St. Mary's JV", "LHS"),
    ("Mount Vernon High School", "Kenwood", "Mount Vernon", "Kenwood"),
    ("Lake Travis High School", "Westlake", "Lake Travis", "Westlake"),
    ("The Woodlands High School - Varsity", "Conroe", "The Woodlands", "Conroe"),
    ("North Hills High School", "NHHS", "North Central", "North Central"),
    ("A&M Consolidated High School", "Bryan", "A&M Rudder", "A&M Rudder"),
    ("A&M Consolidated High School", "Bryan", "A&M Consolidated JV", "Bryan"),
    (None, "LHS", "Strasburg", "Strasburg"),
    ("Strasburg High School", None, "Strasburg", "Strasburg"),
    ("Strasburg High School", "LHS", None, None),
])
def test_opponent_not_owner(owner, team, opp, expected):
    import api_service
    assert api_service._opponent_not_owner(owner, team, opp) == expected


def count_frames(path):
    out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                          "-count_packets", "-show_entries", "stream=nb_read_packets",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


@needs_ffmpeg
def test_download_clips_keeps_every_frame_when_sound_ends_early(tmp_path, served):
    # Live cutup 97367952: -shortest on clips with sound cut 602 video frames.
    root, base = served
    subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(root / "early.mp4")], check=True)
    make_clip(root / "silent.mp4", 2, False)
    make_clip(root / "sound.mp4", 1, True)
    names = ["early.mp4", "silent.mp4", "sound.mp4", "early.mp4"]
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/{n}" for n in names], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    assert count_frames(out) == sum(count_frames(root / n) for n in names)
    assert probe(out)[1] == ["audio", "video"]


@needs_ffmpeg
def test_media_info_counts_frames_and_sees_sound(tmp_path):
    import downloader
    make_clip(tmp_path / "a.mp4", 2, True)
    make_clip(tmp_path / "b.mp4", 1, False)
    assert downloader._media_info(FFMPEG, str(tmp_path / "a.mp4")) == (60, True)
    assert downloader._media_info(FFMPEG, str(tmp_path / "b.mp4")) == (30, False)


def test_cutup_non_https_clip_counts_as_missing(monkeypatch):
    bad = [{"quality": 75, "fileName": "http://169.254.169.254/latest/meta-data"}]
    s = FakeSession(FakeResp(200, {"clipsWithTags": [clip(1), clip(2, files=bad)]}))
    with pytest.raises(ExtractionError, match="1 of 2 plays have no video file"):
        run_extract(monkeypatch, s)


def test_redact_strips_query_from_requests_error_text():
    import downloader
    msg = ("HTTPSConnectionPool(host='vg.hudl.com', port=443): Max retries exceeded with "
           "url: /x/c1.mp4?v=abc&Policy=POLSECRET&Key-Pair-Id=KPSECRET (Caused by X)")
    red = downloader._redact_sensitive(msg)
    assert "POLSECRET" not in red and "KPSECRET" not in red and "url: /x/c1.mp4?" in red


def test_numeric_v_without_team_id_says_so(monkeypatch):
    s = FakeSession(FakeResp(200, {"clipsWithTags": [clip(1)]}))
    with pytest.raises(ExtractionError, match="missing its team number"):
        run_extract(monkeypatch, s, url="https://app.hudl.com/watch/analyze?v=96200833")
    assert not any(c[0] == "GET" for c in s.calls)


def test_non_ascii_digits_do_not_route_to_cutups(monkeypatch):
    s = FakeSession(clips_resp=None)
    monkeypatch.setattr(hudl_mod.HudlExtractor, "_extract_app_hudl_playwright",
                        lambda self, *a, **k: (_ for _ in ()).throw(ExtractionError("pw")))
    with pytest.raises(ExtractionError):
        run_extract(monkeypatch, s, url="https://app.hudl.com/watch/team/14016/analyze?v=%C2%B2&team=14016")
    assert not any(c[0] == "GET" for c in s.calls)



# ── order under real concurrency, job wiring, bad input ──

@pytest.fixture
def served_slow(tmp_path):
    """Like `served`, but any file with 'slow' in its name is held for 1.5 s."""
    import time as _t
    root = tmp_path / "srv_slow"
    root.mkdir()

    class Slow(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def do_GET(self):
            if "slow" in self.path:
                _t.sleep(1.5)
            return super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@needs_ffmpeg
def test_download_clips_first_clip_finishing_last_still_plays_first(tmp_path, served_slow):
    root, base = served_slow
    color_clip(root / "slow_red.mp4", "red", 1)
    color_clip(root / "green.mp4", "green", 1)
    color_clip(root / "blue.mp4", "blue", 1)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/slow_red.mp4", f"{base}/green.mp4", f"{base}/blue.mp4"],
                       str(out), ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    r, g, b = pixel_at(out, 0.5)
    assert r > 150 and g < 100 and b < 100, (r, g, b)
    r, g, b = pixel_at(out, 1.5)
    assert g > 100 and r < 100 and b < 100, (r, g, b)
    r, g, b = pixel_at(out, 2.5)
    assert b > 150 and r < 100, (r, g, b)


def test_media_info_takes_the_final_frame_count(monkeypatch):
    # A long stream copy prints several progress lines; the first is partial.
    import downloader

    class R:
        returncode = 0
        stderr = ("Stream #0:0: Video: h264\nStream #0:1: Audio: aac\n"
                  "frame= 8827 fps=0.0 q=-1.0 size=N/A\rframe=30211 fps=0.0\r"
                  "frame=46116 fps=0.0 q=-1.0 Lsize=N/A time=00:25:39\n")

    monkeypatch.setattr(downloader.subprocess, "run", lambda *a, **k: R())
    assert downloader._media_info("ffmpeg", "x.mp4") == (46116, True)


@pytest.mark.parametrize("body", [None, [1, 2], {"clipsWithTags": [None]},
                                  {"clipsWithTags": [{"angles": [None]}]},
                                  {"clipsWithTags": [clip(1, files=[{"quality": "high",
                                   "fileName": "https://vg.hudl.com/x.mp4"}, {"quality": 2,
                                   "fileName": "https://vg.hudl.com/y.mp4"}])]}])
def test_cutup_malformed_clip_list_is_an_extraction_error(monkeypatch, body):
    with pytest.raises(ExtractionError, match="unexpected format"):
        run_extract(monkeypatch, FakeSession(FakeResp(200, body)))


def test_cutup_string_quality_still_picks_the_best(monkeypatch):
    files = [{"quality": "25", "fileName": "https://vg.hudl.com/x_480.mp4"},
             {"quality": "75", "fileName": "https://vg.hudl.com/x_1080.mp4"}]
    r = run_extract(monkeypatch, FakeSession(FakeResp(200, {"clipsWithTags": [clip(1, files=files)]})))
    assert r.clip_urls == ["https://vg.hudl.com/x_1080.mp4"]


def test_cutup_multi_angle_uses_the_same_angle_every_play(monkeypatch):
    def two_angles(n, first_listed_order):
        a0 = {"angleName": "Sideline", "order": 0, "duration": 10,
              "files": [{"quality": 75, "fileName": f"https://vg.hudl.com/side{n}.mp4"}]}
        a1 = {"angleName": "End Zone", "order": 1, "duration": 10,
              "files": [{"quality": 75, "fileName": f"https://vg.hudl.com/end{n}.mp4"}]}
        return {"clipId": n, "angles": [a0, a1] if first_listed_order == 0 else [a1, a0]}
    body = {"clipsWithTags": [two_angles(1, 0), two_angles(2, 1), two_angles(3, 0)]}
    r = run_extract(monkeypatch, FakeSession(FakeResp(200, body)))
    assert r.clip_urls == [f"https://vg.hudl.com/side{n}.mp4" for n in (1, 2, 3)]


def _job(api_service, url):
    jid = "t" + str(len(api_service.jobs))
    api_service.jobs[jid] = {"job_id": jid, "video_url": url, "platform": "hudl", "user_id": "u",
                             "status": "queued", "percent": 0.0, "created_at": 0, "updated_at": 0}
    return jid


@needs_ffmpeg
def test_process_job_joins_clips_and_fills_teams(tmp_path, monkeypatch):
    import api_service
    from extractors.base import ExtractResult
    from downloader import DownloadProgress

    monkeypatch.setattr(api_service, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(api_service, "NORMALIZE_VIDEO", False)
    monkeypatch.setattr(api_service, "S3_BUCKET", "")
    monkeypatch.setattr(api_service, "SPORTSREELZ_UPLOAD_URL", "")
    res = ExtractResult(title="LHS vs. Strasburg High Schoo 08/15/2026", platform="hudl",
                        clip_urls=["https://vg.hudl.com/a.mp4", "https://vg.hudl.com/b.mp4"],
                        duration=99.0, team_name="Strasburg High School - Boys Varsity Football")
    monkeypatch.setattr(api_service, "extract", lambda url, **k: res)
    seen = {}

    def fake_download(urls, output_path, headers, cb, ffmpeg_path=None):
        seen["urls"] = urls
        make_clip(Path(output_path), 3, True)
        p = DownloadProgress()
        p.status = "done"
        return p

    monkeypatch.setattr(api_service, "download_clips", fake_download)
    jid = _job(api_service, CUTUP_URL)
    api_service._process_job(jid, cookies={"ident": "x"})
    job = api_service.jobs[jid]
    assert job["status"] == "done", job.get("error")
    assert seen["urls"] == res.clip_urls
    assert (job["team_name"], job["opponent_name"], job["game_date"]) == (
        "Strasburg High School - Boys Varsity Football", "LHS", "08/15/2026")
    assert abs(res.duration - 3.0) < 0.2


def test_process_job_unexpected_extract_error_ends_the_job(monkeypatch):
    import api_service

    def boom(url, **k):
        raise AttributeError("'NoneType' object has no attribute 'get' url: /x.mp4?v=SECRETQ")

    monkeypatch.setattr(api_service, "extract", boom)
    jid = _job(api_service, CUTUP_URL)
    api_service._process_job(jid, cookies={"ident": "x"})
    job = api_service.jobs[jid]
    assert job["status"] == "error"
    assert job["error"].startswith("Extraction failed: AttributeError")
    assert "SECRETQ" not in job["error"]



def test_media_info_ignores_counts_from_a_failed_ffmpeg(monkeypatch):
    import downloader

    class R:
        returncode = 1
        stderr = "Stream #0:0: Video: h264\nframe=   10 fps=0.0\nError while decoding\n"

    monkeypatch.setattr(downloader.subprocess, "run", lambda *a, **k: R())
    assert downloader._media_info("ffmpeg", "x.mp4")[0] is None


@needs_ffmpeg
def test_download_clips_refuses_a_join_with_extra_frames(tmp_path, served, monkeypatch):
    import downloader
    root, base = served
    for i in range(2):
        make_clip(root / f"x{i}.mp4", 1, False)
    real = downloader._media_stats

    def lying(ffmpeg_path, path):
        frames, audio, secs = real(ffmpeg_path, path)
        return (frames * 2 if path.endswith("game.mp4") else frames), audio, secs

    monkeypatch.setattr(downloader, "_media_stats", lying)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/x{i}.mp4" for i in range(2)], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "error" and "refusing to ship" in p.error


@needs_ffmpeg
def test_download_clips_uncountable_clip_fails_with_a_clear_reason(tmp_path, served, monkeypatch):
    import downloader
    root, base = served
    make_clip(root / "y0.mp4", 1, False)
    monkeypatch.setattr(downloader, "_media_stats", lambda ffmpeg_path, path: (None, False, None))
    p = download_clips([f"{base}/y0.mp4"], str(tmp_path / "game.mp4"), ffmpeg_path=FFMPEG)
    assert p.status == "error" and "could not count the video frames of clip 1 of 1" in p.error


# ── clips on different video timescales (seen live 13 Oct 2026) ────────

def test_media_stats_takes_the_final_time(monkeypatch):
    import downloader

    class R:
        returncode = 0
        stderr = ("Stream #0:0: Video: h264\n"
                  "frame=  300 fps=0.0 time=00:00:09.96\r"
                  "frame=31825 fps=0.0 q=-1.0 Lsize=N/A time=01:13:08.86\n")

    monkeypatch.setattr(downloader.subprocess, "run", lambda *a, **k: R())
    frames, audio, secs = downloader._media_stats("ffmpeg", "x.mp4")
    assert (frames, audio) == (31825, False)
    assert abs(secs - 4388.86) < 0.01


@needs_ffmpeg
def test_download_clips_joins_mixed_timescales_at_the_right_length(tmp_path, served):
    root, base = served
    for i in range(4):
        make_clip(root / f"m{i}.mp4", 2, True, timescale=15360 if i % 2 == 0 else 90000)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/m{i}.mp4" for i in range(4)], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    assert abs(probe(out)[0] - 8) < 0.3


@needs_ffmpeg
def test_download_clips_refuses_a_join_with_broken_timing(tmp_path, served, monkeypatch):
    import downloader
    root, base = served
    for i in range(2):
        make_clip(root / f"t{i}.mp4", 2, False)
    real = downloader._media_stats

    def stretched(ffmpeg_path, path):
        frames, audio, secs = real(ffmpeg_path, path)
        return frames, audio, (secs * 4 if path.endswith("game.mp4") else secs)

    monkeypatch.setattr(downloader, "_media_stats", stretched)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/t{i}.mp4" for i in range(2)], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "error" and "broken timing" in p.error
    assert not out.exists()



# ── HUDL video editor address for the same cutups ──────────────────────

EDITOR_URL = "https://www.hudl.com/online-video-editor/882621/100614148/"


@pytest.mark.parametrize("url,kind", [
    (EDITOR_URL, "app_hudl"),
    ("https://www.hudl.com/online-video-editor/882621/100614148", "app_hudl"),
    ("https://www.hudl.com/online-video-editor/", "hudl_page"),
    ("https://www.hudl.com/online-video-editor/882621/abc/", "hudl_page"),
    ("https://www.hudl.com/somewhere/online-video-editor/1/2/", "hudl_page"),
])
def test_editor_address_routing(url, kind):
    assert hudl_mod.HudlExtractor()._identify_type(url) == kind


def test_editor_address_reads_that_teams_cutup(monkeypatch):
    s = FakeSession(FakeResp(200, {"clipsWithTags": [clip(1), clip(2)]}),
                    cutups_resp=FakeResp(200, [{"name": "Dash Gueniot 2026 Highlights"}]))
    r = run_extract(monkeypatch, s, url=EDITOR_URL)
    gets = [c[1] for c in s.calls if c[0] == "GET"]
    assert gets == ["https://www.hudl.com/api/v2/classicfacade/team/882621/cutup/100614148/clips"]
    assert r.clip_urls == [f"https://vg.hudl.com/x/c{n}_1080_3000.mp4?v=1" for n in (1, 2)]
    assert r.title == "Dash Gueniot 2026 Highlights"
    assert not any(c[0] == "POST" and "query GetVideo" in c[2] for c in s.calls)


@needs_ffmpeg
def test_download_clips_accepts_many_short_clips_with_b_frames(tmp_path, served):
    # x264 default B-frames: each clip's last packet lands 2 frames before
    # its end, which must not read as broken timing over a 30-play reel.
    root, base = served
    for i in range(30):
        make_clip(root / f"b{i}.mp4", 1, True, timescale=15360 if i % 2 == 0 else 90000)
    out = tmp_path / "game.mp4"
    p = download_clips([f"{base}/b{i}.mp4" for i in range(30)], str(out), ffmpeg_path=FFMPEG)
    assert p.status == "done", p.error
    assert abs(probe(out)[0] - 30) < 1.5
