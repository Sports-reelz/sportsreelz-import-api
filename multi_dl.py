#!/usr/bin/env python3
"""
Multi-Platform Sports Video Downloader
Supports: HUDL, VEO, YouTube, TRACE, PIXELLOT

Usage:
    python multi_dl.py <URL>                        # single download
    python multi_dl.py <URL> -o downloads/          # custom output dir
    python multi_dl.py <URL> -q 1080p               # quality selection
    python multi_dl.py <URL> --s3-bucket BUCKET     # download + upload to S3
    python multi_dl.py <URL> --player-id player123  # tag with player ID in S3
    python multi_dl.py <URL> --cookies cookies.txt  # for auth-required platforms
    python multi_dl.py <URL> --token "Bearer xyz"   # JWT/bearer token
    python multi_dl.py -f urls.txt                  # batch from file
    python multi_dl.py --gui                        # GUI mode
"""

import argparse
import os
import sys
import time
import subprocess
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from extractors import extract, get_platform
from extractors.base import ExtractionError, AuthRequiredError, ExtractResult
from quality import fetch_and_select, format_variants_table
from downloader import HudlDownloader, DownloadProgress
from utils import find_ffmpeg, sanitize_filename, get_unique_filepath, format_size, read_urls_from_file


PLATFORM_COLORS = {
    "hudl":      "\033[35m",   # magenta
    "veo":       "\033[36m",   # cyan
    "youtube":   "\033[31m",   # red
    "trace":     "\033[33m",   # yellow
    "pixellot":  "\033[34m",   # blue
    "unknown":   "\033[37m",   # white
}
RESET = "\033[0m"
BOLD  = "\033[1m"
GREEN = "\033[32m"
RED   = "\033[31m"


def print_banner():
    print()
    print(f"  {BOLD}+--------------------------------------------+{RESET}")
    print(f"  {BOLD}|   Multi-Platform Sports Video Downloader   |{RESET}")
    print(f"  {BOLD}|  HUDL · VEO · YouTube · TRACE · PIXELLOT  |{RESET}")
    print(f"  {BOLD}+--------------------------------------------+{RESET}")
    print()


def platform_label(platform: str) -> str:
    color = PLATFORM_COLORS.get(platform, "")
    return f"{color}{BOLD}[{platform.upper()}]{RESET}"


def download_with_ytdlp(result: ExtractResult, output_path: str,
                         quality: str = "best", cookies_path: str = None,
                         progress_callback=None) -> DownloadProgress:
    """
    Use yt-dlp to download (for YouTube, VEO, and other yt-dlp-native platforms).
    """
    import shutil as _shutil
    progress = DownloadProgress()
    progress.output_path = output_path
    progress.start_time = time.time()
    progress.status = "downloading"

    # FFmpeg is required to merge separate video+audio streams. If it's not
    # available, fall back to pre-merged single-file formats — quality may be
    # lower (YouTube caps pre-merged at 720p) but the download still succeeds.
    ffmpeg_available = _shutil.which("ffmpeg") is not None

    # Format selector strategy: prefer MP4/M4A streams when available, but fall
    # back gracefully through WebM and then to any best video+audio combo.
    # YouTube often serves higher resolutions only in WebM (VP9/AV1), so a
    # strict [ext=mp4] requirement triggers "Requested format is not available"
    # on those videos. yt-dlp's --merge-output-format mp4 still produces a .mp4
    # at the end regardless of the source container.
    if ffmpeg_available:
        # Three-tier fallback for each quality bucket:
        #   1. Try MP4/M4A (cleanest, no transcode on merge)
        #   2. Try any video+audio at that height
        #   3. Try any best single-file at that height
        if quality == "720p":
            fmt = ("bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                   "bestvideo[height<=720]+bestaudio/"
                   "best[height<=720]/best")
        elif quality == "480p":
            fmt = ("bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
                   "bestvideo[height<=480]+bestaudio/"
                   "best[height<=480]/best")
        elif quality == "1080p":
            fmt = ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                   "bestvideo[height<=1080]+bestaudio/"
                   "best[height<=1080]/best")
        else:
            fmt = ("bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                   "bestvideo+bestaudio/best")
    else:
        # No FFmpeg available — must use pre-merged single-file formats.
        # YouTube caps pre-merged at 720p so 1080p falls back to 720p.
        if quality == "720p":
            fmt = "best[height<=720][ext=mp4]/best[height<=720]/best"
        elif quality == "480p":
            fmt = "best[height<=480][ext=mp4]/best[height<=480]/best"
        elif quality == "1080p":
            # Pre-merged 1080p rarely exists on YouTube; cap at 720p when
            # FFmpeg cannot merge separate streams.
            fmt = "best[height<=720][ext=mp4]/best[height<=720]/best"
        else:
            fmt = "best[ext=mp4]/best"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--newline",   # Progress on new lines for easier parsing
    ]

    # YouTube bot-detection mitigation. YouTube's default web player client
    # is the most aggressively gated against datacenter IPs. Forcing yt-dlp
    # to try the tv-embedded and ios clients first (in addition to web)
    # routes the request through endpoints that historically have weaker
    # bot-detection coverage. Honors YT_DLP_PLAYER_CLIENT for operators
    # who want to customise the priority list per deployment.
    is_youtube = (
        "youtube.com" in (result.source_url or result.direct_url or "")
        or "youtu.be" in (result.source_url or result.direct_url or "")
    )
    if is_youtube:
        player_clients = os.environ.get(
            "YT_DLP_PLAYER_CLIENT",
            "tv_embedded,ios,android,web_safari,web",
        )
        # YouTube's 2026 PO Token gate: even with the lower-gated clients,
        # YouTube can refuse formats that don't carry a Proof-of-Origin token.
        # `formats=missing_pot` tells yt-dlp to keep formats that lack a PO
        # token so they can still be downloaded. This is the documented
        # escape valve for datacenter-IP deployments without a bgutil
        # PO Token provider sidecar.
        youtube_formats = os.environ.get(
            "YT_DLP_YOUTUBE_FORMATS",
            "missing_pot",
        )
        cmd += [
            "--extractor-args",
            (
                f"youtube:player_client={player_clients}"
                f";formats={youtube_formats}"
            ),
        ]

    # Cookie handling. YouTube has been rolling out aggressive bot-detection
    # (the "Sign in to confirm you're not a bot" error). Pass a cookies file
    # from a signed-in browser to bypass it.
    #
    # Priority:
    #   1. cookies_path passed in by the caller (per-request)
    #   2. YT_DLP_COOKIES_FILE env var (applies to all yt-dlp downloads)
    #   3. YOUTUBE_COOKIES_FILE env var (legacy name, same behavior)
    #   4. YT_DLP_COOKIES_FROM_BROWSER env var (e.g. "chrome", "firefox") —
    #      uses Playwright/yt-dlp browser-cookie extraction on the host
    effective_cookies = cookies_path or os.environ.get("YT_DLP_COOKIES_FILE") \
        or os.environ.get("YOUTUBE_COOKIES_FILE")
    if effective_cookies and os.path.isfile(effective_cookies):
        cmd += ["--cookies", effective_cookies]

    cookies_from_browser = os.environ.get("YT_DLP_COOKIES_FROM_BROWSER")
    if cookies_from_browser and not effective_cookies:
        cmd += ["--cookies-from-browser", cookies_from_browser]

    # Add source-specific headers
    for k, v in (result.headers or {}).items():
        cmd += ["--add-header", f"{k}:{v}"]

    cmd.append(result.source_url or result.direct_url)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        import re
        for line in proc.stdout:
            line = line.strip()
            # Parse yt-dlp progress: [download]  xx.x% of ...
            m = re.search(r'\[download\]\s+([\d.]+)%', line)
            if m:
                progress.percent = float(m.group(1))
                # Extract speed and ETA
                speed_m = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
                eta_m = re.search(r'ETA\s+([\d:]+)', line)
                if speed_m:
                    progress.speed = speed_m.group(1)
                if eta_m:
                    progress.eta = eta_m.group(1)
                if progress_callback:
                    progress_callback(progress)

        proc.wait()

        # If yt-dlp succeeded but output_path is missing, look for an alternate
        # output file in the same directory — yt-dlp sometimes writes with
        # different extensions when format selection or merging behaves
        # unexpectedly. If a usable file is found, rename it to output_path.
        if proc.returncode == 0 and not os.path.isfile(output_path):
            out_dir = os.path.dirname(output_path) or "."
            base_name = os.path.splitext(os.path.basename(output_path))[0]
            candidate = None
            try:
                for f in os.listdir(out_dir):
                    full = os.path.join(out_dir, f)
                    if not os.path.isfile(full):
                        continue
                    if f.startswith(base_name) and f.lower().endswith(
                        (".mp4", ".mkv", ".webm", ".m4v", ".mov")
                    ):
                        if candidate is None or os.path.getsize(full) > os.path.getsize(candidate):
                            candidate = full
            except Exception:
                candidate = None
            if candidate and os.path.getsize(candidate) > 1024:
                try:
                    if candidate != output_path:
                        os.replace(candidate, output_path)
                except Exception:
                    output_path = candidate
                    progress.output_path = output_path

        if proc.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 1024:
            progress.status = "done"
            progress.percent = 100.0
            progress.size = format_size(os.path.getsize(output_path))
            elapsed = time.time() - progress.start_time
            progress.time_elapsed = f"{int(elapsed//60)}m {int(elapsed%60)}s"
        else:
            progress.status = "error"
            if proc.returncode == 0:
                if not ffmpeg_available:
                    progress.error = (
                        "Download completed but output file not produced. "
                        "FFmpeg was not detected on the system — install FFmpeg "
                        "and add it to PATH, then retry."
                    )
                else:
                    progress.error = "Download completed but output file not found at the expected path."
            else:
                progress.error = f"yt-dlp exited with code {proc.returncode}"

    except FileNotFoundError:
        progress.status = "error"
        progress.error = "yt-dlp not found. Install: pip install yt-dlp"
    except Exception as e:
        progress.status = "error"
        progress.error = str(e)

    if progress_callback:
        progress_callback(progress)
    return progress


def download_single(url: str, output_dir: str, quality: str, ffmpeg_path: str,
                    cookies=None, session_token: str = None,
                    s3_bucket: str = None, game_id: str = None,
                    player_id: str = None,
                    s3_public: bool = False, s3_prefix: str = "") -> bool:
    """Download a single URL. Returns True on success."""

    platform = get_platform(url)
    print(f"  {platform_label(platform)} {url[:80]}{'...' if len(url) > 80 else ''}")
    print()

    # ── Step 1: Extract ──────────────────────────────────────────────
    print("  [1/3] Extracting video info...")

    cookies_dict = None
    if isinstance(cookies, dict):
        cookies_dict = cookies
    elif isinstance(cookies, str) and os.path.isfile(cookies):
        # Pass path to yt-dlp; for requests-based extractors, parse it
        cookies_dict = _parse_cookies_file(cookies)

    try:
        result = extract(url, cookies=cookies_dict, session_token=session_token)
    except AuthRequiredError as e:
        print(f"\n  {RED}AUTH REQUIRED:{RESET} {e}")
        return False
    except ExtractionError as e:
        print(f"\n  {RED}ERROR:{RESET} {e}")
        return False

    print(f"  Title:    {result.title}")
    print(f"  Platform: {result.platform.upper()}")
    if result.m3u8_url:
        print(f"  Stream:   {result.m3u8_url[:70]}...")
    elif result.direct_url:
        print(f"  URL:      {result.direct_url[:70]}...")
    print()

    # ── Step 2: Quality selection (HLS only) ─────────────────────────
    selected_url = result.m3u8_url
    variant = None
    all_variants = []

    if result.m3u8_url and not result.use_ytdlp:
        print("  [2/3] Checking available qualities...")
        try:
            selected_url, variant, all_variants = fetch_and_select(
                result.m3u8_url, result.headers, result.base_url, quality
            )
            if all_variants:
                print(f"  Found {len(all_variants)} quality options:")
                print(format_variants_table(all_variants))
                if variant:
                    print(f"  Selected: {variant.name}")
        except Exception as e:
            print(f"  WARNING: Quality check failed ({e}), using default stream")
            selected_url = result.m3u8_url
        print()
    else:
        print(f"  [2/3] Quality: {quality} (handled by yt-dlp)")
        print()

    # ── Step 3: Download ─────────────────────────────────────────────
    filename = sanitize_filename(result.title) + ".mp4"
    output_path = get_unique_filepath(output_dir, filename)

    print(f"  [3/3] Downloading -> {os.path.basename(output_path)}")
    print()

    def _progress(prog: DownloadProgress):
        if prog.status == "downloading":
            bar_width = 30
            filled = int(bar_width * prog.percent / 100)
            bar = "█" * filled + "░" * (bar_width - filled)
            eta = f" ETA {prog.eta}" if prog.eta else ""
            speed = f" {prog.speed}" if prog.speed else ""
            print(f"\r  [{bar}] {prog.percent:5.1f}%{speed}{eta}  ", end="", flush=True)

    start = time.time()

    if result.use_ytdlp:
        # YouTube, VEO, and other yt-dlp-native platforms
        cookies_path = cookies if isinstance(cookies, str) and os.path.isfile(cookies) else None
        progress = download_with_ytdlp(result, output_path, quality, cookies_path, _progress)
    else:
        # HLS via FFmpeg (HUDL, PIXELLOT, TRACE)
        dl = HudlDownloader(ffmpeg_path)
        progress = dl.download(selected_url, output_path, result.headers, _progress)

    print()  # newline after progress bar
    elapsed = time.time() - start

    if progress.status != "done":
        print(f"  {RED}FAILED:{RESET} {progress.error}")
        return False

    size_str = format_size(os.path.getsize(output_path))
    print(f"  {GREEN}DONE!{RESET} {size_str} in {int(elapsed//60)}m {int(elapsed%60)}s")
    print(f"  Saved: {output_path}")

    # ── Step 4: S3 Upload (optional) ─────────────────────────────────
    if s3_bucket:
        print()
        print("  [4/4] Uploading to AWS S3...")
        try:
            from uploader import S3Uploader
            uploader = S3Uploader(bucket=s3_bucket, prefix=s3_prefix)

            upload_progress = [0]
            total_size = os.path.getsize(output_path)

            def _s3_progress(uploaded, total):
                pct = (uploaded / total * 100) if total else 0
                bar_width = 30
                filled = int(bar_width * pct / 100)
                bar = "█" * filled + "░" * (bar_width - filled)
                print(f"\r  [{bar}] {pct:5.1f}% uploaded  ", end="", flush=True)

            s3_url = uploader.upload(
                local_path=output_path,
                game_id=game_id,
                player_id=player_id,
                platform=result.platform,
                public=s3_public,
                progress_callback=_s3_progress,
            )
            print()
            print(f"  {GREEN}S3 DONE!{RESET} {s3_url}")
            return True

        except ImportError:
            print(f"  {RED}ERROR:{RESET} boto3 not installed. Run: pip install boto3")
            return False
        except Exception as e:
            print(f"  {RED}S3 ERROR:{RESET} {e}")
            return False

    return True


def _parse_cookies_file(path: str) -> dict:
    """Parse a Netscape-format cookies.txt file into a dict."""
    cookies = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    except Exception:
        pass
    return cookies


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Platform Sports Video Downloader (HUDL, VEO, YouTube, TRACE, PIXELLOT)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Platform examples:
  HUDL (fan page):   fan.hudl.com/broadcast/watch?b=...
  HUDL (app):        app.hudl.com/watch/team/.../analyze?v=...
  VEO:               app.veo.co/matches/20251205-12425.../
  YouTube:           youtube.com/watch?v=... or youtu.be/...
  TRACE:             traceup.com/...  (needs --cookies or --token)
  PIXELLOT:          pixellot.tv/...  (needs --token)

Examples:
  python multi_dl.py "https://fan.hudl.com/broadcast/watch?b=..." -o downloads/
  python multi_dl.py "https://app.veo.co/matches/..." -q 1080p
  python multi_dl.py "https://youtube.com/watch?v=..." -q 720p
  python multi_dl.py "https://app.hudl.com/watch/team/.../analyze?v=..." --cookies hudl.txt
  python multi_dl.py "https://app.veo.co/..." --s3-bucket my-bucket --player-id player123
""",
    )
    parser.add_argument("urls", nargs="*", help="Video URLs to download")
    parser.add_argument("-f", "--file",   help="File with URLs (.txt, .csv, .xlsx)")
    parser.add_argument("-o", "--output", default="downloads", help="Output directory (default: downloads)")
    parser.add_argument("-q", "--quality", default="best",
                        help="Quality: best, 1080p, 720p, 480p, worst (default: best)")
    parser.add_argument("--cookies",  help="Path to cookies.txt (Netscape format) for auth-required platforms")
    parser.add_argument("--token",    help="Bearer/JWT token for TRACE or PIXELLOT")
    parser.add_argument("--ffmpeg",   help="Path to FFmpeg binary (auto-detected if not set)")
    parser.add_argument("--gui",      action="store_true", help="Launch GUI mode")

    # S3 options
    s3 = parser.add_argument_group("AWS S3 Upload (optional)")
    s3.add_argument("--s3-bucket",    help="S3 bucket name (default: S3_INPUT_BUCKET env var)")
    s3.add_argument("--s3-prefix",    default="", help="S3 key prefix (default: none)")
    s3.add_argument("--s3-public",    action="store_true", help="Make S3 object publicly readable")
    s3.add_argument("--game-id",      help="SPORTSREELZ game ID (e.g., 19800). Uploads as {gameId}/file-{ts}-{name}")
    s3.add_argument("--player-id",    help="Player account ID (legacy key format)")

    args = parser.parse_args()

    if args.gui:
        try:
            from gui import launch_gui
            launch_gui()
        except ImportError as e:
            print(f"GUI launch failed: {e}")
            sys.exit(1)
        return

    print_banner()

    # Find FFmpeg (needed for HLS platforms)
    ffmpeg_path = None
    try:
        ffmpeg_path = args.ffmpeg or find_ffmpeg()
        print(f"  FFmpeg:  {ffmpeg_path}")
    except FileNotFoundError:
        print("  FFmpeg:  NOT FOUND (YouTube/VEO still work via yt-dlp)")

    # Collect URLs
    urls = list(args.urls)
    if args.file:
        try:
            file_urls = read_urls_from_file(args.file)
            print(f"  Loaded {len(file_urls)} URL(s) from: {args.file}")
            urls.extend(file_urls)
        except FileNotFoundError:
            print(f"  ERROR: File not found: {args.file}")
            sys.exit(1)

    if not urls:
        parser.print_help()
        sys.exit(0)

    # Show platform detection
    print()
    for url in urls:
        p = get_platform(url)
        print(f"  {platform_label(p):30s} {url[:70]}{'...' if len(url) > 70 else ''}")

    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)
    print()
    print(f"  Output:  {output_dir}")
    print()

    # Download
    results = []
    for i, url in enumerate(urls):
        if len(urls) > 1:
            print(f"  {'─'*50}")
            print(f"  [{i+1}/{len(urls)}] Processing...")
            print(f"  {'─'*50}")
            print()

        success = download_single(
            url=url,
            output_dir=output_dir,
            quality=args.quality,
            ffmpeg_path=ffmpeg_path,
            cookies=args.cookies,
            session_token=args.token,
            s3_bucket=args.s3_bucket or os.environ.get("S3_INPUT_BUCKET"),
            game_id=args.game_id,
            player_id=args.player_id,
            s3_public=args.s3_public,
            s3_prefix=args.s3_prefix,
        )
        results.append(success)
        print()

    # Summary for batch
    if len(urls) > 1:
        done = sum(results)
        failed = len(results) - done
        print(f"  {'─'*50}")
        print(f"  {GREEN}Completed: {done}/{len(urls)}{RESET}", end="")
        if failed:
            print(f"  {RED}  Failed: {failed}{RESET}", end="")
        print()

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
