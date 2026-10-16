"""
Hudl Video Downloader
FFmpeg-based HLS download with real-time progress tracking.
"""

import os
import re
import subprocess
import threading
import time
from utils import find_ffmpeg, format_size, format_duration, format_speed, get_unique_filepath


# Patterns for redacting secrets out of subprocess output before it is
# surfaced in API responses, jobs.json, or logs. FFmpeg and yt-dlp echo the
# signed input URL (tokens live in the query string) and -headers/--add-header
# values (Cookie, Authorization) into their stderr on failure.
#
# We mask the secret VALUE only — not the whole line — so that diagnostically
# useful context (file paths like /app/cookies/x.txt, exception types like
# PermissionError, the host/path of a URL) stays visible. Dropping whole lines
# was hiding the actual error (e.g. a traceback whose path contained "cookie").
# Also matches requests' "Max retries exceeded with url: /path?query" form,
# which carries no scheme.
_URL_QUERY_RE = re.compile(r"((?:https?://|\burl: /)[^\s?'\"]*)\?[^\s'\"]*")
_HEADER_VALUE_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|x-cookie)\b(\s*[:=]\s*).+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_PARAM_VALUE_RE = re.compile(
    r"(?i)\b(token|signature|sig|password|secret|api[_-]?key|x-amz-[a-z0-9-]*)=([^&\s'\"]+)"
)


def _redact_sensitive(line: str) -> str:
    """Mask secret values in a single line of subprocess output while keeping
    the surrounding diagnostic text (paths, exception types, host/path) intact.
    """
    if not line:
        return line
    # 1. Strip query strings off URLs — signed S3/CDN tokens live there.
    line = _URL_QUERY_RE.sub(r"\1?<redacted>", line)
    # 2. Mask auth/cookie header values, keeping the header name.
    line = _HEADER_VALUE_RE.sub(r"\1\2<redacted>", line)
    # 3. Mask bearer tokens.
    line = _BEARER_RE.sub("Bearer <redacted>", line)
    # 4. Mask sensitive key=value params (token=, signature=, x-amz-*=, ...).
    line = _PARAM_VALUE_RE.sub(r"\1=<redacted>", line)
    return line


class DownloadProgress:
    """Tracks download progress state."""
    def __init__(self):
        self.status = "waiting"  # waiting, downloading, muxing, done, error, cancelled
        self.percent = 0.0
        self.speed = ""
        self.size = ""
        self.time_elapsed = ""
        self.eta = ""
        self.error = ""
        self.output_path = ""
        self.start_time = 0.0

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "percent": self.percent,
            "speed": self.speed,
            "size": self.size,
            "time_elapsed": self.time_elapsed,
            "eta": self.eta,
            "error": self.error,
            "output_path": self.output_path,
        }


class HudlDownloader:
    """Downloads HLS streams using FFmpeg with progress tracking."""

    def __init__(self, ffmpeg_path: str = None):
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg()
        self._cancel_event = threading.Event()
        self._process = None

    def download(self, m3u8_url: str, output_path: str, headers: dict = None,
                 progress_callback=None) -> DownloadProgress:
        """
        Download an HLS stream to MP4.

        Args:
            m3u8_url: URL of the m3u8 playlist (master or media)
            output_path: Full path for the output .mp4 file
            headers: HTTP headers dict (Referer, User-Agent, etc.)
            progress_callback: Optional callable(DownloadProgress) for updates

        Returns:
            DownloadProgress with final state
        """
        self._cancel_event.clear()
        progress = DownloadProgress()
        progress.output_path = output_path
        progress.start_time = time.time()
        progress.status = "downloading"

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # Build FFmpeg command
        cmd = self._build_ffmpeg_cmd(m3u8_url, output_path, headers)

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )

            # FFmpeg writes progress to stderr
            self._read_progress(self._process, progress, progress_callback)

            returncode = self._process.wait()

            if self._cancel_event.is_set():
                progress.status = "cancelled"
                progress.error = "Download cancelled by user"
                self._cleanup_partial(output_path)
            elif returncode != 0:
                stderr_tail = self._get_stderr_tail(self._process)
                progress.status = "error"
                progress.error = f"FFmpeg exited with code {returncode}: {stderr_tail}"
            else:
                # Verify output file exists and has content
                if os.path.isfile(output_path) and os.path.getsize(output_path) > 1024:
                    progress.status = "done"
                    progress.percent = 100.0
                    progress.size = format_size(os.path.getsize(output_path))
                    elapsed = time.time() - progress.start_time
                    progress.time_elapsed = format_duration(elapsed)
                else:
                    progress.status = "error"
                    progress.error = "Output file is empty or missing"

        except FileNotFoundError:
            progress.status = "error"
            progress.error = f"FFmpeg not found at: {self.ffmpeg_path}"
        except Exception as e:
            progress.status = "error"
            progress.error = str(e)
        finally:
            self._process = None

        if progress_callback:
            progress_callback(progress)

        return progress

    def cancel(self):
        """Cancel the current download."""
        self._cancel_event.set()
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass

    def _build_ffmpeg_cmd(self, m3u8_url: str, output_path: str, headers: dict) -> list:
        """Build the FFmpeg command line."""
        cmd = [self.ffmpeg_path]

        # Global options
        cmd += ["-y"]  # Overwrite output
        cmd += ["-loglevel", "info"]
        cmd += ["-stats"]

        # HTTP headers for HLS
        if headers:
            header_str = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
            cmd += ["-headers", header_str]

        # Input
        cmd += ["-i", m3u8_url]

        # Copy streams (no re-encoding)
        cmd += ["-c", "copy"]

        # MP4 container options
        cmd += ["-movflags", "+faststart"]
        cmd += ["-bsf:a", "aac_adtstoasc"]

        # Output
        cmd += [output_path]

        return cmd

    def _read_progress(self, proc, progress: DownloadProgress, callback):
        """Parse FFmpeg stderr for progress info."""
        stderr_data = b""
        duration_seconds = None
        # Ring buffer of the most recent stderr bytes. FFmpeg writes its
        # final error message (e.g. "No space left on device", "Cannot
        # allocate memory", "Conversion failed", codec errors) to stderr
        # right before it exits. We retain the tail here so _get_stderr_tail
        # can surface it — the pipe itself is already drained to EOF by the
        # time the caller checks the return code, so re-reading it returns
        # nothing. Without this the error field was always empty, which is
        # why every "FFmpeg exited with code N: " had no reason attached.
        self._stderr_tail = b""

        while True:
            if self._cancel_event.is_set():
                break

            chunk = proc.stderr.read(256)
            if not chunk:
                break

            stderr_data += chunk
            # Retain a generous tail (last ~8KB) across the whole run so the
            # final error survives even after the progress buffer is trimmed.
            self._stderr_tail = (self._stderr_tail + chunk)[-8192:]
            text = stderr_data.decode("utf-8", errors="replace")

            # Try to extract total duration from stream info
            if duration_seconds is None:
                dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", text)
                if dur_match:
                    h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), int(dur_match.group(3))
                    duration_seconds = h * 3600 + m * 60 + s

            # Parse progress lines (FFmpeg outputs \r-terminated lines)
            lines = text.split("\r")
            for line in lines:
                self._parse_progress_line(line, progress, duration_seconds)

            if callback and progress.status == "downloading":
                callback(progress)

            # Keep only last 4KB of stderr to avoid memory bloat
            if len(stderr_data) > 4096:
                stderr_data = stderr_data[-2048:]

    def _parse_progress_line(self, line: str, progress: DownloadProgress, duration: float):
        """Parse a single FFmpeg progress output line."""
        # Match: size=   12345kB or size=   12345KiB time=00:01:23.45 bitrate=...
        size_match = re.search(r"size=\s*(\d+)\s*(?:kB|KiB)", line)
        time_match = re.search(r"time=\s*(\d+):(\d+):(\d+)\.(\d+)", line)
        speed_match = re.search(r"speed=\s*([\d.]+)x", line)
        bitrate_match = re.search(r"bitrate=\s*([\d.]+)\s*kbits/s", line)

        if time_match:
            h = int(time_match.group(1))
            m = int(time_match.group(2))
            s = int(time_match.group(3))
            current_seconds = h * 3600 + m * 60 + s

            elapsed = time.time() - progress.start_time
            progress.time_elapsed = format_duration(elapsed)

            # Calculate percent if we know duration
            if duration and duration > 0:
                progress.percent = min(99.9, (current_seconds / duration) * 100)

                # ETA
                if progress.percent > 0:
                    total_est = elapsed / (progress.percent / 100)
                    remaining = total_est - elapsed
                    progress.eta = format_duration(remaining)

        if size_match:
            size_kb = int(size_match.group(1))
            progress.size = format_size(size_kb * 1024)

        if speed_match:
            spd = float(speed_match.group(1))
            progress.speed = f"{spd:.1f}x"

    def _get_stderr_tail(self, proc) -> str:
        """Get the last bit of stderr for error messages.

        Returns the tail buffered by _read_progress during the run. The pipe
        is already at EOF here (the progress reader drained it), so reading
        proc.stderr again yields nothing — we must use the retained buffer.
        We also attempt one final read in case any bytes arrived after the
        reader loop exited, appending them to the buffered tail.
        """
        buffered = getattr(self, "_stderr_tail", b"") or b""
        try:
            remaining = proc.stderr.read()
            if remaining:
                buffered = (buffered + remaining)[-8192:]
        except Exception:
            pass
        if not buffered:
            return ""
        text = buffered.decode("utf-8", errors="replace")
        # Collapse FFmpeg's \r-heavy progress noise to newlines, then return
        # the last few non-empty lines — that's where the real error lives.
        # Redact first: FFmpeg echoes the (signed) input URL and -headers
        # values (Cookie/Authorization) into stderr on HTTP errors, and this
        # text flows into the API-visible error field, jobs.json, and logs.
        text = text.replace("\r", "\n")
        lines = [_redact_sensitive(ln.strip()) for ln in text.split("\n") if ln.strip()]
        tail = " | ".join(lines[-4:]) if lines else ""
        return tail[-500:]

    def _cleanup_partial(self, path: str):
        """Remove partial download file."""
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


def download_single(m3u8_url: str, output_path: str, headers: dict = None,
                    progress_callback=None, ffmpeg_path: str = None) -> DownloadProgress:
    """Convenience function for a single download."""
    dl = HudlDownloader(ffmpeg_path)
    return dl.download(m3u8_url, output_path, headers, progress_callback)


def _media_stats(ffmpeg_path: str, path: str):
    """(video frame count or None, has an audio stream, video seconds or None),
    by stream-copying the video to the null muxer: no decode. The frame count
    is exact where durations are not (HUDL's listed clip durations overstate
    the footage); the seconds are where the last video frame lands, which is
    what catches a join whose timestamps came out stretched."""
    r = subprocess.run([ffmpeg_path, "-hide_banner", "-i", path,
                        "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    err = r.stderr or ""
    ok = r.returncode == 0
    m = re.findall(r"frame=\s*(\d+)", err)
    frames = int(m[-1]) if ok and m else None
    t = re.findall(r"time=\s*(-?)(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    seconds = None
    if ok and t:
        sign, h, mi, s = t[-1]
        seconds = (-1 if sign else 1) * (int(h) * 3600 + int(mi) * 60 + float(s))
    return frames, re.search(r"Stream #\S+.*: Audio:", err) is not None, seconds


def _container_seconds(ffmpeg_path: str, path: str):
    """The file's Duration as ffmpeg reports it: what the concat demuxer
    advances by for each file, so the joined timeline can be predicted."""
    r = subprocess.run([ffmpeg_path, "-hide_banner", "-i", path], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def _media_info(ffmpeg_path: str, path: str):
    """(video frame count or None, has an audio stream)."""
    frames, has_audio, _ = _media_stats(ffmpeg_path, path)
    return frames, has_audio


def download_clips(urls: list, output_path: str, headers: dict = None,
                   progress_callback=None, ffmpeg_path: str = None,
                   workers: int = 4, retries: int = 3) -> DownloadProgress:
    """Download an ordered list of MP4 clips and join them into one file.

    HUDL cutups have no HLS manifest, only one MP4 per play. The video of a
    cutup's clips shares codec parameters and is joined without re-encoding.
    Some clips have sound and some do not, so every clip gets exactly one AAC
    track (its own, or silence) before the join; the output always has sound,
    like every other game video the downstream transcoder receives.
    """
    import shutil
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed

    prog = DownloadProgress()
    prog.status = "downloading"
    prog.start_time = time.time()
    prog.output_path = output_path
    ffmpeg_path = ffmpeg_path or find_ffmpeg()
    total = len(urls)
    # Inside the job's own directory, not named after the game title.
    parts_dir = os.path.join(os.path.dirname(output_path) or ".", ".clip_parts")

    def report():
        elapsed = time.time() - prog.start_time
        prog.time_elapsed = format_duration(elapsed)
        if progress_callback:
            progress_callback(prog)

    def fetch(i, url):
        path = os.path.join(parts_dir, f"{i:04d}.mp4")
        last = None
        for attempt in range(retries):
            try:
                with requests.get(url, headers=headers or {}, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    expected = int(r.headers.get("Content-Length") or 0)
                    with open(path, "wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
                size = os.path.getsize(path)
                if size == 0 or (expected and size != expected):
                    raise IOError(f"got {size} of {expected} bytes")
                return path, size
            except Exception as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(
            f"clip {i + 1} of {total} failed after {retries} tries: "
            f"{_redact_sensitive(str(last))}"
        )

    try:
        if not urls:
            raise RuntimeError("no clips to download")
        os.makedirs(parts_dir, exist_ok=True)
        paths, done, nbytes = [None] * total, 0, 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch, i, u): i for i, u in enumerate(urls)}
            for fut in as_completed(futures):
                try:
                    paths[futures[fut]], size = fut.result()
                except Exception:
                    for f in futures:
                        f.cancel()
                    raise
                done += 1
                nbytes += size
                elapsed = time.time() - prog.start_time
                prog.percent = round(done / total * 95, 1)
                prog.size = format_size(nbytes)
                prog.speed = format_speed(nbytes / elapsed if elapsed > 0 else 0)
                report()

        prog.status = "muxing"
        report()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def ffmpeg_err(r):
            return " | ".join(_redact_sensitive(ln) for ln in (r.stderr or "").splitlines()[-4:])

        # The concat demuxer needs every input to carry the same streams. One
        # live cutup mixed 59 clips with sound and 46 without, and joining
        # them as-is silently dropped 94 s of the game. So each clip is first
        # rewritten with its video copied and exactly one AAC track: its own
        # sound re-encoded to a common format, or silence when it has none.
        # -shortest only on the silence: on clips with sound it cut the video
        # wherever the sound ended first (602 frames over 58 clips, live).
        # One video timescale for every clip: a live cutup mixed 1/90000 and
        # 1/15360 clips, and the stream-copy join stretched 1040 s of video
        # over 4389 s while the frame count still matched.
        def uniform(i):
            src = paths[i]
            frames, has_audio, _ = _media_stats(ffmpeg_path, src)
            if not frames:
                raise RuntimeError(
                    f"could not count the video frames of clip {i + 1} of {total} "
                    "(no video, or ffmpeg printed no frame count)"
                )
            dst = os.path.join(parts_dir, f"u{i:04d}.mp4")
            cmd = [ffmpeg_path, "-y", "-loglevel", "error", "-i", src]
            if has_audio:
                cmd += ["-map", "0:v:0", "-map", "0:a:0"]
            else:
                cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            cmd += ["-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    "-b:a", "128k", "-video_track_timescale", "90000", dst]
            r = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
            if r.returncode != 0 or not os.path.isfile(dst):
                raise RuntimeError(f"preparing clip {i + 1} of {total} failed: {ffmpeg_err(r)}")
            os.remove(src)
            _, _, secs = _media_stats(ffmpeg_path, dst)
            return dst, frames, (secs or 0.0, _container_seconds(ffmpeg_path, dst) or 0.0)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            prepared = list(ex.map(uniform, range(total)))
        expected = sum(f for _, f, _ in prepared)
        # Where the last frame should land: each earlier clip advances the
        # join by its container duration, the last adds its own last frame.
        expected_secs = sum(d for _, _, (_, d) in prepared[:-1]) + prepared[-1][2][0]

        # Bare names, resolved next to list.txt: no quoting, no -safe 0.
        list_path = os.path.join(parts_dir, "list.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            for p, _, _ in prepared:
                fh.write(f"file '{os.path.basename(p)}'\n")
        cmd = [ffmpeg_path, "-y", "-loglevel", "error",
               "-f", "concat", "-i", list_path,
               "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
               "-movflags", "+faststart", output_path]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
        if r.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(f"joining {total} clips failed (ffmpeg exit {r.returncode}): {ffmpeg_err(r)}")

        # A join that loses footage or stretches it still exits 0, so check
        # both the frame count and where the last frame lands.
        joined, _, joined_secs = _media_stats(ffmpeg_path, output_path)
        if joined != expected:
            raise RuntimeError(
                f"joined video has {joined or 0} frames but the {total} clips have "
                f"{expected}; refusing to ship an incomplete game"
            )
        if joined_secs is None or abs(joined_secs - expected_secs) > max(2.0, expected_secs * 0.01):
            raise RuntimeError(
                f"joined video runs {joined_secs or 0:.1f}s but the {total} clips run "
                f"{expected_secs:.1f}s; refusing to ship a game with broken timing"
            )

        prog.status = "done"
        prog.percent = 100.0
        prog.size = format_size(os.path.getsize(output_path))
        report()
    except Exception as e:
        prog.status = "error"
        prog.error = str(e)[:500]
        try:
            if os.path.isfile(output_path):
                os.remove(output_path)
        except Exception:
            pass
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)
    return prog
