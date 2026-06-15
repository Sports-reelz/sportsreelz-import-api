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
_SECRET_LINE_RE = re.compile(
    r"(?i)(authorization|cookie|set-cookie|x-api-key|bearer|token=|signature=|x-amz-)"
)
_URL_QUERY_RE = re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*")


def _redact_sensitive(line: str) -> str:
    """Redact secrets from a single line of subprocess output.

    - Any line mentioning an auth header / token / AWS-signature param is
      dropped entirely (it is never safe to surface).
    - Otherwise, strip the query string off any URL so signed S3/CDN tokens
      don't leak while keeping the host/path useful for diagnosis.
    """
    if not line:
        return line
    # Strip query strings off URLs first — signed S3/CDN tokens (X-Amz-*,
    # signature=, token=) live there. Doing this first preserves the useful
    # host/path for diagnosis instead of dropping the whole line.
    line = _URL_QUERY_RE.sub(r"\1?<redacted>", line)
    # Then drop any line still carrying a bare auth header or token (e.g. an
    # echoed Cookie: / Authorization: header, or a non-URL token=...).
    if _SECRET_LINE_RE.search(line):
        return "<redacted: contained credentials>"
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
