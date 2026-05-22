"""
SPORTSREELZ Video Import API Service

Unified API for downloading videos from HUDL, VEO, and Trace,
then uploading them to the SPORTSREELZ platform.

Endpoints:
    POST /api/import          — Start a video import job
    POST /api/import/verify   — Submit Trace magic code (2-step auth)
    GET  /api/import/status/{job_id} — Check job progress
    GET  /api/health          — Health check

Run:
    uvicorn api_service:app --host 0.0.0.0 --port 8000
"""

import os
import uuid
import time
import logging
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from extractors import extract, get_platform
from extractors.base import ExtractionError, AuthRequiredError, detect_platform
from extractors.trace import TraceAuthManager
from downloader import HudlDownloader, DownloadProgress
from quality import fetch_and_select
from utils import find_ffmpeg, sanitize_filename

# ── Configuration ─────────────────────────────────────────────────────────────

API_KEY = os.environ.get("API_KEY", "changeme")
SPORTSREELZ_UPLOAD_URL = os.environ.get("SPORTSREELZ_UPLOAD_URL", "")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "3"))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", tempfile.mkdtemp(prefix="sreelz_"))
NORMALIZE_VIDEO = os.environ.get("NORMALIZE_VIDEO", "true").lower() == "true"

# Direct S3 upload — when S3_BUCKET is configured, the downloaded video is
# uploaded straight to the bucket and download_url is set to the resulting
# (presigned) S3 URL. If only SPORTSREELZ_UPLOAD_URL is configured, the
# webhook path is used instead.
S3_BUCKET = os.environ.get("S3_BUCKET", "") or os.environ.get("S3_INPUT_BUCKET", "")
S3_PUBLIC = os.environ.get("S3_PUBLIC", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api_service")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SPORTSREELZ Video Import Service",
    description="Download videos from HUDL/VEO/Trace and upload to SPORTSREELZ",
    version="1.0.0",
)

# ── OpenAPI (Swagger) API-key auth ─────────────────────────────────────────────

def custom_openapi():
    """
    Add `X-API-Key` auth to the Swagger UI via an OpenAPI security scheme.
    This makes the "Authorize" button show up in `/docs`.
    """
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    components = openapi_schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }

    # Apply security to all /api/* endpoints except /api/health
    for path, path_item in openapi_schema.get("paths", {}).items():
        if path == "/api/health" or not path.startswith("/api/"):
            continue
        for method, op in (path_item or {}).items():
            if not isinstance(op, dict):
                continue
            op.setdefault("security", [{"ApiKeyAuth": []}])

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

# ── State ─────────────────────────────────────────────────────────────────────

jobs: dict[str, dict] = {}
pending_verifications: dict[str, dict] = {}
trace_auth = TraceAuthManager()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

try:
    ffmpeg_path = find_ffmpeg()
except FileNotFoundError:
    ffmpeg_path = "ffmpeg"


# ── Auth Middleware ───────────────────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        # Skip auth for health check
        if request.url.path == "/api/health":
            return await call_next(request)
        api_key = request.headers.get("X-API-Key")
        if api_key != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or missing API key"},
            )
    return await call_next(request)


# ── Models ────────────────────────────────────────────────────────────────────

class ImportRequest(BaseModel):
    user_id: str = Field(..., description="SPORTSREELZ user ID")
    video_url: str = Field(..., description="Video URL from HUDL/VEO/Trace")
    platform: Optional[str] = Field(None, description="Platform (auto-detected if omitted)")
    platform_email: Optional[str] = Field(None, description="Platform login email")
    platform_password: Optional[str] = Field(None, description="Platform login password")
    quality: str = Field("1080p", description="Video quality: 1080p, 720p, best")


class VerifyRequest(BaseModel):
    job_id: str = Field(..., description="Job ID from /api/import response")
    magic_code: str = Field(..., description="6-digit magic code from email")


class ImportResponse(BaseModel):
    job_id: str
    status: str
    message: str


class StatusResponse(BaseModel):
    job_id: str
    status: str
    platform: str
    title: Optional[str] = None
    percent: float = 0.0
    speed: Optional[str] = None
    error: Optional[str] = None
    download_url: Optional[str] = None
    created_at: float
    updated_at: float


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "active_jobs": len([j for j in jobs.values() if j["status"] not in ("done", "error")]),
        "total_jobs": len(jobs),
    }


@app.post("/api/import", response_model=ImportResponse)
async def start_import(req: ImportRequest):
    """
    Start a video import job.
    For Trace: returns status='pending_verification' — follow up with /api/import/verify.
    For HUDL/VEO: starts download immediately.
    """
    job_id = str(uuid.uuid4())[:8]
    platform = req.platform or detect_platform(req.video_url)

    if platform == "unknown":
        raise HTTPException(400, f"Could not detect platform from URL: {req.video_url}")

    job = {
        "job_id": job_id,
        "status": "queued",
        "platform": platform,
        "user_id": req.user_id,
        "video_url": req.video_url,
        "quality": req.quality,
        "title": None,
        "percent": 0.0,
        "speed": None,
        "error": None,
        "download_url": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    jobs[job_id] = job

    # ── Trace: 2-step magic code auth ─────────────────────────────────
    if platform == "trace":
        if not req.platform_email:
            job["status"] = "error"
            job["error"] = "Trace requires platform_email for magic code login"
            raise HTTPException(400, "Trace requires platform_email")

        # Check for existing valid session
        saved_cookies = trace_auth.get_session(req.platform_email)
        if saved_cookies:
            log.info(f"[{job_id}] Trace: reusing saved session for {req.platform_email}")
            job["status"] = "queued"
            executor.submit(_process_job, job_id, cookies=saved_cookies)
            return ImportResponse(
                job_id=job_id, status="queued",
                message="Download started (using saved Trace session)",
            )

        # Request magic code
        try:
            trace_auth.request_magic_code(req.platform_email)
        except Exception as e:
            log.warning(f"[{job_id}] Magic code request failed: {e}")

        job["status"] = "pending_verification"
        pending_verifications[job_id] = {
            "email": req.platform_email,
            "request": req.model_dump(),
        }

        return ImportResponse(
            job_id=job_id, status="pending_verification",
            message=f"Magic code sent to {req.platform_email}. "
                    f"Submit code via POST /api/import/verify",
        )

    # ── HUDL: email + password auth ───────────────────────────────────
    if platform == "hudl":
        cookies = None
        if req.platform_email and req.platform_password:
            try:
                from hudl_auth import ensure_valid_cookies
                cookies_path = ensure_valid_cookies(
                    req.platform_email, req.platform_password
                )
                # Parse cookies file to dict
                cookies = _parse_cookies_file(cookies_path)
            except Exception as e:
                job["status"] = "error"
                job["error"] = f"HUDL login failed: {e}"
                return ImportResponse(
                    job_id=job_id, status="error", message=str(e),
                )

        executor.submit(_process_job, job_id, cookies=cookies)
        return ImportResponse(
            job_id=job_id, status="queued", message="Download started",
        )

    # ── VEO: email + password or public ───────────────────────────────
    if platform == "veo":
        session_token = None
        if req.platform_password:
            session_token = req.platform_password  # VEO uses bearer token as password

        executor.submit(_process_job, job_id, session_token=session_token)
        return ImportResponse(
            job_id=job_id, status="queued", message="Download started",
        )

    # ── Other platforms ───────────────────────────────────────────────
    executor.submit(_process_job, job_id)
    return ImportResponse(
        job_id=job_id, status="queued", message="Download started",
    )


@app.post("/api/import/verify", response_model=ImportResponse)
async def verify_magic_code(req: VerifyRequest):
    """
    Submit Trace magic code to complete authentication.
    Call this after receiving status='pending_verification' from /api/import.
    """
    if req.job_id not in pending_verifications:
        raise HTTPException(404, f"No pending verification for job {req.job_id}")

    pending = pending_verifications[req.job_id]
    email = pending["email"]
    job = jobs.get(req.job_id)

    if not job:
        raise HTTPException(404, f"Job {req.job_id} not found")

    # Try API-based verification first, then browser fallback
    cookies = None
    try:
        cookies = trace_auth.submit_magic_code(email, req.magic_code)
    except Exception:
        try:
            cookies = trace_auth.login_with_browser(email, req.magic_code)
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"Magic code verification failed: {e}"
            return ImportResponse(
                job_id=req.job_id, status="error", message=str(e),
            )

    if not cookies:
        job["status"] = "error"
        job["error"] = "Verification returned no session"
        return ImportResponse(
            job_id=req.job_id, status="error",
            message="Magic code verification failed — try again",
        )

    # Success — start the download
    del pending_verifications[req.job_id]
    job["status"] = "queued"
    job["updated_at"] = time.time()

    executor.submit(_process_job, req.job_id, cookies=cookies)

    return ImportResponse(
        job_id=req.job_id, status="queued",
        message="Verification successful, download started",
    )


@app.get("/api/import/status/{job_id}", response_model=StatusResponse)
async def get_status(job_id: str):
    """Check the status of an import job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    return StatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        platform=job["platform"],
        title=job.get("title"),
        percent=job.get("percent", 0.0),
        speed=job.get("speed"),
        error=job.get("error"),
        download_url=job.get("download_url"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )


# ── Background Worker ─────────────────────────────────────────────────────────

def _process_job(job_id: str, cookies: dict = None, session_token: str = None):
    """
    Background worker: extract → download → normalize → upload.
    """
    job = jobs[job_id]
    url = job["video_url"]
    quality = job.get("quality", "1080p")

    log.info(f"[{job_id}] Starting {job['platform'].upper()} download: {url[:80]}")

    # ── Extract ───────────────────────────────────────────────────────
    job["status"] = "extracting"
    job["updated_at"] = time.time()

    try:
        result = extract(url, cookies=cookies, session_token=session_token)
    except AuthRequiredError as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        log.error(f"[{job_id}] Auth required: {e}")
        return
    except ExtractionError as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        log.error(f"[{job_id}] Extraction failed: {e}")
        return

    job["title"] = result.title
    log.info(f"[{job_id}] Extracted: {result.title} ({result.platform})")

    # ── Download ──────────────────────────────────────────────────────
    job["status"] = "downloading"
    job["updated_at"] = time.time()

    filename = sanitize_filename(result.title) + ".mp4"
    output_path = os.path.join(DOWNLOAD_DIR, job_id, filename)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def _on_progress(prog: DownloadProgress):
        job["percent"] = prog.percent
        job["speed"] = prog.speed
        job["updated_at"] = time.time()

    try:
        if result.use_ytdlp:
            from multi_dl import download_with_ytdlp
            progress = download_with_ytdlp(
                result, output_path, quality,
                progress_callback=_on_progress,
            )
        elif result.m3u8_url:
            # Quality selection
            selected_url = result.m3u8_url
            try:
                selected_url, variant, _ = fetch_and_select(
                    result.m3u8_url, result.headers, result.base_url, quality
                )
                if variant:
                    log.info(f"[{job_id}] Selected quality: {variant.name}")
            except Exception:
                selected_url = result.m3u8_url

            dl = HudlDownloader(ffmpeg_path)
            progress = dl.download(
                selected_url, output_path, result.headers, _on_progress,
            )
        else:
            job["status"] = "error"
            job["error"] = "No downloadable URL found"
            job["updated_at"] = time.time()
            return

    except Exception as e:
        job["status"] = "error"
        job["error"] = f"Download failed: {e}"
        job["updated_at"] = time.time()
        log.error(f"[{job_id}] Download error: {e}")
        return

    if progress.status != "done":
        job["status"] = "error"
        job["error"] = progress.error or "Download failed"
        job["updated_at"] = time.time()
        log.error(f"[{job_id}] Download failed: {progress.error}")
        return

    file_size = os.path.getsize(output_path)
    log.info(f"[{job_id}] Downloaded: {file_size / 1024 / 1024:.1f} MB")

    # ── Normalize to 1080p 16:9 ───────────────────────────────────────
    if NORMALIZE_VIDEO:
        job["status"] = "processing"
        job["updated_at"] = time.time()
        log.info(f"[{job_id}] Normalizing to 1080p 16:9...")

        normalized_path = output_path.replace(".mp4", "_normalized.mp4")
        success = _normalize_video(output_path, normalized_path)

        if success and os.path.isfile(normalized_path):
            os.replace(normalized_path, output_path)
            log.info(f"[{job_id}] Normalized: {os.path.getsize(output_path) / 1024 / 1024:.1f} MB")
        else:
            log.warning(f"[{job_id}] Normalization failed, using original")

    # ── Upload (S3 direct, or SPORTSREELZ webhook) ────────────────────
    if S3_BUCKET:
        job["status"] = "uploading"
        job["percent"] = 0.0
        job["updated_at"] = time.time()
        log.info(f"[{job_id}] Uploading to S3 bucket '{S3_BUCKET}'...")

        try:
            from uploader import S3Uploader

            file_size = os.path.getsize(output_path)

            def _s3_progress(bytes_uploaded, total_bytes):
                if total_bytes:
                    job["percent"] = round((bytes_uploaded / total_bytes) * 100, 1)
                    job["updated_at"] = time.time()

            uploader = S3Uploader(bucket=S3_BUCKET)
            s3_url = uploader.upload(
                local_path=output_path,
                game_id=str(job["user_id"]),
                platform=result.platform,
                public=S3_PUBLIC,
                progress_callback=_s3_progress,
            )
            job["download_url"] = s3_url
            log.info(f"[{job_id}] Uploaded to S3 ({file_size / 1024 / 1024:.1f} MB)")

        except Exception as e:
            log.warning(f"[{job_id}] S3 upload failed: {e} (video still saved locally)")
            job["error"] = f"S3 upload failed: {e}"

    elif SPORTSREELZ_UPLOAD_URL:
        job["status"] = "uploading"
        job["percent"] = 0.0
        job["updated_at"] = time.time()
        log.info(f"[{job_id}] Uploading to SPORTSREELZ webhook...")

        try:
            import requests as req
            with open(output_path, "rb") as f:
                files = {"video": (filename, f, "video/mp4")}
                data = {
                    "userID": job["user_id"],
                    "title": result.title,
                    "platform": result.platform,
                }
                if result.duration:
                    data["duration"] = str(int(result.duration))

                resp = req.post(
                    SPORTSREELZ_UPLOAD_URL,
                    files=files,
                    data=data,
                    headers={"X-API-Key": API_KEY},
                    timeout=600,  # 10 min for large uploads
                )

                if resp.status_code in (200, 201):
                    body = resp.json() if resp.text else {}
                    game_id = body.get("gameId", "unknown")
                    log.info(f"[{job_id}] Uploaded! gameId={game_id}")
                    job["download_url"] = body.get("url", "")
                else:
                    log.warning(f"[{job_id}] Upload returned {resp.status_code}: {resp.text[:200]}")

        except Exception as e:
            log.warning(f"[{job_id}] Upload failed: {e} (video still saved locally)")

    # ── Done ──────────────────────────────────────────────────────────
    job["status"] = "done"
    job["percent"] = 100.0
    job["updated_at"] = time.time()
    log.info(f"[{job_id}] Complete: {result.title}")

    # Cleanup temp files after upload completes
    try:
        job_dir = os.path.dirname(output_path)
        if (S3_BUCKET or SPORTSREELZ_UPLOAD_URL) and os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass


# ── Utilities ─────────────────────────────────────────────────────────────────

def _normalize_video(input_path: str, output_path: str) -> bool:
    """
    Re-encode video to 1080p 16:9 standard format.
    Ensures consistent output for the SPORTSREELZ platform.
    """
    import subprocess

    cmd = [
        ffmpeg_path, "-y",
        "-i", input_path,
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
               "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,  # 30 min max
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


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


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    log.info(f"Starting SPORTSREELZ Import Service on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
