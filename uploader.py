"""
AWS S3 uploader for imported sports videos.

Uploads a local file to S3 and returns the public/signed URL.
Designed for SPORTSREELZ backend: video gets downloaded locally,
then uploaded to S3 matching their existing structure:

    {gameId}/file-{timestamp}-{original_filename}

Staging bucket: sportsreelz-input
Production bucket: sreelz-input

Requirements:
    pip install boto3

Configuration (pass directly or via environment variables):
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_REGION          (default: us-east-1)
    S3_BUCKET           (your bucket name)
"""

import os
import time
import threading
from pathlib import Path
from typing import Optional, Callable


class S3UploadError(Exception):
    pass


class S3Uploader:
    """
    Upload videos to AWS S3.

    Usage (SPORTSREELZ style):
        uploader = S3Uploader(bucket="sportsreelz-input")
        url = uploader.upload(
            local_path="/tmp/game.mp4",
            game_id="19800",
        )
        # Uploads to: 19800/file-1710000000-game.mp4

    Usage (custom key):
        url = uploader.upload(
            local_path="/tmp/game.mp4",
            s3_key="custom/path/game.mp4",
        )
    """

    def __init__(
        self,
        bucket: str = None,
        aws_access_key_id: str = None,
        aws_secret_access_key: str = None,
        region: str = None,
        prefix: str = "",                 # no prefix by default (gameId is the top-level folder)
    ):
        self.bucket = bucket or os.environ.get("S3_BUCKET") or os.environ.get("S3_INPUT_BUCKET")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.prefix = prefix

        _key = aws_access_key_id or os.environ.get("AWS_ACCESS_KEY_ID")
        _secret = aws_secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY")

        if not self.bucket:
            raise S3UploadError(
                "S3 bucket not configured. Set bucket= or S3_BUCKET/S3_INPUT_BUCKET env var."
            )

        try:
            import boto3
            self._s3 = boto3.client(
                "s3",
                region_name=self.region,
                aws_access_key_id=_key,
                aws_secret_access_key=_secret,
            )
        except ImportError:
            raise S3UploadError(
                "boto3 is required for S3 uploads. Install: pip install boto3"
            )

    def upload(
        self,
        local_path: str,
        s3_key: str = None,
        game_id: str = None,
        player_id: str = None,
        platform: str = "unknown",
        public: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """
        Upload a video file to S3.

        Key generation priority:
          1. Explicit s3_key (full control)
          2. game_id → SPORTSREELZ format: {gameId}/file-{timestamp}-{filename}
          3. player_id → legacy format: {prefix}{player_id}/{platform}/{filename}
          4. Fallback: {prefix}{platform}/{filename}

        Args:
            local_path: Full path to the local video file.
            s3_key: Explicit S3 object key (overrides auto-generation).
            game_id: SPORTSREELZ game ID (e.g., "19800"). Generates key in their format.
            player_id: Player account ID (legacy key format).
            platform: Source platform name (hudl/veo/youtube/etc).
            public: If True, object is publicly readable. If False, returns presigned URL.
            progress_callback: Called with (bytes_uploaded, total_bytes) during upload.

        Returns:
            Public URL (if public=True) or presigned URL (valid 7 days).

        Raises:
            S3UploadError on failure.
        """
        local_path = Path(local_path)
        if not local_path.exists():
            raise S3UploadError(f"File not found: {local_path}")

        file_size = local_path.stat().st_size
        filename = local_path.name

        # Build S3 key — match SPORTSREELZ pattern: {gameId}/file-{timestamp}-{filename}
        if not s3_key:
            if game_id:
                ts = int(time.time() * 1000)  # millisecond timestamp (matches their pattern)
                s3_key = f"{self.prefix}{game_id}/file-{ts}-{filename}"
            elif player_id:
                s3_key = f"{self.prefix}{player_id}/{platform}/{filename}"
            else:
                s3_key = f"{self.prefix}{platform}/{filename}"

        # Detect content type from extension
        ext = local_path.suffix.lower()
        content_types = {
            ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
            ".mkv": "video/x-matroska", ".webm": "video/webm", ".3gp": "video/3gpp",
        }
        content_type = content_types.get(ext, "video/mp4")

        # Progress tracking callback for boto3
        uploaded = [0]

        def _boto_progress(bytes_amount):
            uploaded[0] += bytes_amount
            if progress_callback:
                progress_callback(uploaded[0], file_size)

        try:
            extra_args = {}
            if public:
                extra_args["ACL"] = "public-read"
            extra_args["ContentType"] = content_type
            extra_args["Metadata"] = {
                "source-platform": platform,
                "player-id": player_id or "",
                "game-id": game_id or "",
                "original-filename": filename,
                "uploaded-by": "sportsreelz-importer",
            }

            self._s3.upload_file(
                str(local_path),
                self.bucket,
                s3_key,
                ExtraArgs=extra_args,
                Callback=_boto_progress,
            )

        except Exception as e:
            raise S3UploadError(f"S3 upload failed: {e}")

        if public:
            return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{s3_key}"
        else:
            # Presigned URL valid for 7 days
            url = self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": s3_key},
                ExpiresIn=604800,  # 7 days
            )
            return url

    def upload_async(
        self,
        local_path: str,
        s3_key: str = None,
        player_id: str = None,
        platform: str = "unknown",
        public: bool = False,
        on_done: Optional[Callable[[str, Optional[Exception]], None]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> threading.Thread:
        """
        Non-blocking version of upload(). Runs in background thread.

        Args:
            on_done: Called with (s3_url, error) when complete.
                     error is None on success, Exception on failure.

        Returns:
            The background thread (already started).
        """
        def _worker():
            try:
                url = self.upload(
                    local_path=local_path,
                    s3_key=s3_key,
                    player_id=player_id,
                    platform=platform,
                    public=public,
                    progress_callback=progress_callback,
                )
                if on_done:
                    on_done(url, None)
            except Exception as e:
                if on_done:
                    on_done(None, e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def delete(self, s3_key: str) -> bool:
        """Delete an object from S3. Returns True on success."""
        try:
            self._s3.delete_object(Bucket=self.bucket, Key=s3_key)
            return True
        except Exception:
            return False

    def exists(self, s3_key: str) -> bool:
        """Check if an S3 object exists."""
        try:
            self._s3.head_object(Bucket=self.bucket, Key=s3_key)
            return True
        except Exception:
            return False
