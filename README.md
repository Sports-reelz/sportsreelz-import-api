# SPORTSREELZ Video Import Service

Multi-platform video importer for the SPORTSREELZ platform. Downloads game footage from HUDL, VEO, Trace, YouTube, and Pixellot, then uploads it to your AWS S3 input bucket using your existing path convention.

Built as a standalone REST API — your frontend posts a URL, gets a `job_id`, polls for status, and gets back an S3 URL when complete.

---

## What's Inside

| File | Purpose |
| ---- | ------- |
| `api_service.py` | FastAPI service — all endpoints |
| `extractors/` | Per-platform extraction logic (HUDL, VEO, Trace, YouTube, Pixellot) |
| `hudl_auth.py` | HUDL Playwright login + JWT capture |
| `downloader.py` | HLS / MP4 download + FFmpeg muxing |
| `uploader.py` | AWS S3 uploader with progress callbacks |
| `quality.py` | Quality selection (1080p / 720p / best) |
| `extractor.py` | Master extractor router |
| `utils.py` | FFmpeg detection, filename sanitizing |
| `Dockerfile` | Production image (Python 3.11 + FFmpeg + Playwright) |
| `docker-compose.yml` | One-command deploy |
| `.env.example` | Configuration template |
| `API_DOCS.txt` | Full endpoint reference |

---

## Quick Start (Docker — Recommended)

```bash
git clone https://github.com/PyotrMuhammad/sportsreelz-video-importer.git
cd sportsreelz-video-importer
cp .env.example .env
# Edit .env — fill in API_KEY and AWS credentials
docker-compose up -d
```

API is live on `http://localhost:8000`. Visit `http://localhost:8000/docs` for interactive Swagger UI.

---

## Quick Start (Without Docker)

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Edit .env
uvicorn api_service:app --host 0.0.0.0 --port 8000
```

Requires Python 3.11+ and FFmpeg installed on the host.

---

## Configuration

Edit `.env`:

```ini
# Required
API_KEY=your-secure-key
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
S3_BUCKET=sportsreelz-input        # staging, or sreelz-input for production

# Optional
SPORTSREELZ_UPLOAD_URL=            # webhook callback URL (leave empty for polling)
MAX_WORKERS=3                      # max concurrent downloads
NORMALIZE_VIDEO=false              # set false to skip re-encoding (recommended — let MediaConvert handle it)
```

---

## Endpoints

All `/api/*` endpoints (except `/api/health`) require:
```
X-API-Key: <your-key>
```

### `POST /api/import` — Start a job

```json
{
  "user_id": "player_123",
  "video_url": "https://www.hudl.com/video/3/15987654/62e3a6b0",
  "platform": "hudl",
  "platform_email": "player@email.com",
  "platform_password": "playerpass",
  "quality": "1080p"
}
```

### `POST /api/import/verify` — Submit Trace magic code

```json
{
  "job_id": "a1b2c3d4",
  "magic_code": "482916"
}
```

### `GET /api/import/status/{job_id}` — Poll progress

```json
{
  "job_id": "a1b2c3d4",
  "status": "downloading",
  "platform": "hudl",
  "title": "Game vs Rival - Apr 10, 2026",
  "percent": 45.2,
  "speed": "2.3 MB/s",
  "error": null,
  "download_url": null
}
```

Status flow: `queued → extracting → downloading → processing → uploading → done` (or `error` / `pending_verification`).

### `GET /api/health` — No auth

Full reference: see `API_DOCS.txt`.

---

## Supported Platforms

| Platform | URL Domains | Auth |
| -------- | ----------- | ---- |
| HUDL | `hudl.com`, `app.hudl.com` | Email + password |
| VEO | `app.veo.co` | None (public matches). platform_email / platform_password are not used for VEO |
| Trace | `traceup.com`, `tracevision.com` | Email → magic code (2-step) |
| YouTube | `youtube.com`, `youtu.be` | None |
| Pixellot | `pixellot.tv` | None |

---

## S3 Upload Pattern

Videos are uploaded to your S3 bucket as:
```
{gameId}/file-{unix_timestamp_ms}-{original_filename}
```

This matches your existing pipeline — MediaConvert picks files up from the input bucket exactly as it does today. **No changes needed to your processing flow.**

---

## Integration Flow

**Standard (HUDL / VEO / YouTube / Pixellot):**

1. Frontend posts the game URL to `/api/import`, plus platform_email / platform_password for HUDL only (VEO, YouTube and Pixellot take the URL alone; VEO imports public matches)
2. API returns `job_id` immediately (or `error` if credentials wrong)
3. Frontend polls `/api/import/status/{job_id}` every 3 seconds
4. When `status === "done"`, video is in S3 — link to player profile

**Trace (2-step):**

1. Frontend posts URL + email to `/api/import`
2. API returns `status: "pending_verification"` (magic code sent to player's email)
3. Player enters 6-digit code
4. Frontend posts code to `/api/import/verify`
5. Continue with standard polling

Trace sessions cache for 30 days — only first import needs the magic code.

---

## Error Handling

All errors return synchronously when the API can detect them immediately:

| Scenario | Response |
| -------- | -------- |
| Bad API key | `401 Unauthorized` |
| Wrong HUDL credentials | `status: "error"`, `error: "HUDL login failed: invalid credentials"` |
| Wrong Trace magic code | `status: "error"`, `error: "Magic code verification failed — try again"` |
| Bad URL | `status: "error"`, `error: "yt-dlp failed: ..."` |
| Private VEO match | `status: "error"`, `error: "VEO requires authentication..."` |

Async errors (network failures during download) surface in the same `error` field once polling detects them.

---

## Production Deployment

**Server requirements:** Linux VPS / EC2 / DigitalOcean, 2GB RAM minimum, Docker installed.

**Recommended subdomain:** `videoimport.sportsreelz.com` → server IP.

**Logs:** `docker-compose logs -f api` for live tail.

**Restart:** `docker-compose restart api`.

**Updates:** `git pull && docker-compose up -d --build`.

---

## Support

Questions during integration — ping me on Fiverr. Happy to clarify anything that's unclear or jump on a quick call if Syed gets stuck.

— Py'otr
