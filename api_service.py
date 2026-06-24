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
import re
import uuid
import time
import json
import logging
import tempfile
import shutil
import threading
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

# Persistent job state — without this, jobs disappear on every API restart
# (docker-compose restart, container redeploy, worker crash) and the client
# polling for job status gets "Job not found" even though the underlying
# download may have completed and the file is already in S3.
#
# Default path uses ./data/jobs.json relative to the working directory.
# Override with JOBS_FILE to point at a path that survives container redeploys
# (e.g. a Docker named volume or bind mount).
JOBS_FILE = os.environ.get("JOBS_FILE", os.path.join(os.getcwd(), "data", "jobs.json"))
JOBS_PERSIST_INTERVAL = float(os.environ.get("JOBS_PERSIST_INTERVAL", "2.0"))

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


# ── Job persistence ───────────────────────────────────────────────────────────
# Survives API restarts so clients polling for job_id status don't see
# "Job not found" responses for jobs that started before the restart.

_jobs_lock = threading.Lock()
_jobs_dirty = False


def _serialize_jobs() -> dict:
    """Filter jobs dict to only JSON-serialisable fields (no thread locks,
    futures, callbacks, etc.) so it can be written to disk safely."""
    out = {}
    for jid, job in jobs.items():
        clean = {}
        for k, v in job.items():
            if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                clean[k] = v
        out[jid] = clean
    return out


def _persist_jobs():
    """Atomically write the current jobs dict to disk."""
    global _jobs_dirty
    try:
        os.makedirs(os.path.dirname(JOBS_FILE) or ".", exist_ok=True)
        payload = _serialize_jobs()
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, JOBS_FILE)
        _jobs_dirty = False
    except Exception as e:
        log.warning(f"Failed to persist jobs to {JOBS_FILE}: {e}")


def _load_jobs():
    """Restore jobs from disk at startup. Jobs that were in non-terminal
    states (queued, downloading, processing, uploading, pending_verification)
    when the previous process died are marked as 'error' with a clear message
    rather than being left in a state that can never make progress."""
    if not os.path.isfile(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        terminal = {"done", "error"}
        recovered = 0
        marked_stale = 0
        for jid, jdata in (loaded or {}).items():
            if not isinstance(jdata, dict):
                continue
            status = jdata.get("status")
            if status not in terminal:
                jdata["status"] = "error"
                jdata["error"] = (
                    jdata.get("error")
                    or "Job state lost — the API process restarted while this "
                       "job was in flight. Re-submit /api/import to retry."
                )
                jdata["updated_at"] = time.time()
                marked_stale += 1
            jobs[jid] = jdata
            recovered += 1
        log.info(
            f"Restored {recovered} jobs from {JOBS_FILE} "
            f"({marked_stale} were in-flight and marked stale)"
        )
    except Exception as e:
        log.warning(f"Failed to load jobs from {JOBS_FILE}: {e}")


def _persistence_loop():
    """Background thread: writes jobs to disk at most every
    JOBS_PERSIST_INTERVAL seconds, but only when something changed."""
    last_snapshot_hash = None
    while True:
        try:
            time.sleep(JOBS_PERSIST_INTERVAL)
            with _jobs_lock:
                snapshot_hash = hash(json.dumps(
                    _serialize_jobs(), sort_keys=True, ensure_ascii=False
                ))
                if snapshot_hash != last_snapshot_hash:
                    _persist_jobs()
                    last_snapshot_hash = snapshot_hash
        except Exception:
            # Persistence is best-effort. If anything in the loop fails,
            # log nothing (would spam) and try again next tick.
            pass


@app.on_event("startup")
def _on_startup():
    _load_jobs()
    t = threading.Thread(target=_persistence_loop, daemon=True, name="jobs-persist")
    t.start()


@app.on_event("shutdown")
def _on_shutdown():
    # Flush one last time so anything updated in the last
    # JOBS_PERSIST_INTERVAL is captured before the process exits.
    _persist_jobs()


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
    # Best-effort match metadata parsed from the title/platform. Null when the
    # source doesn't expose it (e.g. an arbitrary YouTube title with no opponent).
    team_name: Optional[str] = None
    opponent_name: Optional[str] = None
    game_date: Optional[str] = None
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
        team_name=job.get("team_name"),
        opponent_name=job.get("opponent_name"),
        game_date=job.get("game_date"),
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
    team, opponent, game_date = _parse_match_meta(result.title, result.platform, result)
    job["team_name"] = team
    job["opponent_name"] = opponent
    job["game_date"] = game_date
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

    # ── Upload: S3 first (file landing), then SPORTSREELZ webhook ─────
    # When both are configured, the file lands in S3 and the resulting S3
    # URL is then posted to SPORTSREELZ_UPLOAD_URL as a URL-pointer so the
    # downstream transcoder (MediaConvert) can fetch it. When only one is
    # configured, only that path fires.
    s3_url = None
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

    if SPORTSREELZ_UPLOAD_URL:
        job["status"] = "uploading"
        job["updated_at"] = time.time()
        log.info(f"[{job_id}] Notifying SPORTSREELZ webhook...")

        try:
            import requests as req

            # jobId is the correlation key the webhook receiver uses to
            # match this notification back to the /api/import request that
            # triggered it. Without it the receiver can't disambiguate
            # multiple concurrent imports from the same user (one user can
            # have several jobs in flight at once).
            payload = {
                "jobId": job_id,
                "videoUrl": s3_url,
                "userID": job["user_id"],
                "title": result.title,
                "teamName": job.get("team_name"),
                "opponentName": job.get("opponent_name"),
                "gameDate": job.get("game_date"),
                "platform": result.platform,
                "createdAt": job.get("created_at"),
            }
            if result.duration:
                payload["duration"] = int(result.duration)

            if s3_url:
                # File is already in S3 — post URL pointer as JSON so the
                # transcode-from-url endpoint can pull it.
                resp = req.post(
                    SPORTSREELZ_UPLOAD_URL,
                    json=payload,
                    headers={
                        "X-API-Key": API_KEY,
                        "Content-Type": "application/json",
                    },
                    timeout=60,
                )
            else:
                # No S3 upload happened — fall back to multipart file upload
                # for backward compatibility with deployments that don't use
                # S3 at all. jobId is included as a form field so the
                # receiver can still correlate even without the JSON path.
                with open(output_path, "rb") as f:
                    files = {"video": (filename, f, "video/mp4")}
                    data = {
                        "jobId": job_id,
                        "userID": str(job["user_id"]),
                        "title": result.title,
                        "platform": result.platform,
                    }
                    if job.get("team_name"):
                        data["teamName"] = job["team_name"]
                    if job.get("opponent_name"):
                        data["opponentName"] = job["opponent_name"]
                    if job.get("game_date"):
                        data["gameDate"] = job["game_date"]
                    created_at = job.get("created_at")
                    if created_at is not None:
                        data["createdAt"] = str(created_at)
                    if result.duration:
                        data["duration"] = str(int(result.duration))
                    resp = req.post(
                        SPORTSREELZ_UPLOAD_URL,
                        files=files,
                        data=data,
                        headers={"X-API-Key": API_KEY},
                        timeout=600,
                    )

            if resp.status_code in (200, 201):
                body = {}
                try:
                    body = resp.json() if resp.text else {}
                except Exception:
                    body = {}
                game_id = body.get("gameId") or body.get("game_id") or "unknown"
                webhook_url = body.get("url") or body.get("videoUrl") or body.get("download_url")
                log.info(
                    f"[{job_id}] Webhook accepted "
                    f"(success={body.get('success')}, gameId={game_id})"
                )
                # Webhook response URL takes precedence over the S3 URL if
                # the transcoder returns its own canonical URL.
                if webhook_url:
                    job["download_url"] = webhook_url
            else:
                log.warning(
                    f"[{job_id}] Webhook returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                )

        except Exception as e:
            log.warning(f"[{job_id}] Webhook notification failed: {e}")

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


_VS_SPLIT_RE = re.compile(r"\s+(?:vs?\.?|versus)\s+", re.IGNORECASE)
_DATE_RES = [
    # 2026-04-10  /  2026/04/10
    re.compile(r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b"),
    # 04/10/2026  /  4-10-26
    re.compile(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b"),
    # Apr 10, 2026  /  April 10 2026
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,)?\s+\d{4})\b",
        re.IGNORECASE,
    ),
    # 10 Apr 2026
    re.compile(
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b",
        re.IGNORECASE,
    ),
]


def _parse_match_meta(title, platform, result):
    """Best-effort extraction of (team_name, opponent_name, game_date) from a
    video title. Returns (None, None, None) for any piece that can't be parsed
    confidently — we never guess. Titles like "Knob Noster girls soccer v.
    Pleasant Hill - Apr 10, 2026" split cleanly; arbitrary YouTube titles
    usually yield nulls, which is correct.
    """
    team = opponent = game_date = None
    if not title:
        return team, opponent, game_date

    text = str(title).strip()

    # 1. Pull a date out of the title if present, and remove it so it doesn't
    #    contaminate the opponent name.
    for rx in _DATE_RES:
        m = rx.search(text)
        if m:
            game_date = m.group(1).strip().rstrip(",")
            text = (text[: m.start()] + text[m.end():]).strip()
            break

    # 2. Strip common trailing separators left after date removal.
    text = re.sub(r"[\s\-–—|]+$", "", text).strip()

    # 3. Split "TeamA vs TeamB" into team / opponent.
    parts = _VS_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        team = parts[0].strip(" -–—|") or None
        opponent = parts[1].strip(" -–—|") or None

    return team, opponent, game_date


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
