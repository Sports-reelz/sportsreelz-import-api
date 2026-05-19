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

        while True:
            if self._cancel_event.is_set():
                break

            chunk = proc.stderr.read(256)
            if not chunk:
                break

            stderr_data += chunk
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
        """Get the last bit of stderr for error messages."""
        try:
            remaining = proc.stderr.read()
            if remaining:
                text = remaining.decode("utf-8", errors="replace")
                # Return last 200 chars
                return text.strip()[-200:]
        except Exception:
            pass
        return ""

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
