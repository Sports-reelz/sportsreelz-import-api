FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Runtime defaults (override via env / compose)
ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/app/downloads \
    BGUTIL_BASE_URL=http://bgutil-provider:4416

# Create mount points + ensure cookies path is a file
RUN mkdir -p /app/data /app/cookies /app/downloads \
    && rm -rf /app/cookies/youtube_cookies.txt \
    && touch /app/cookies/youtube_cookies.txt

# Declare mount points (bind these via docker-compose volumes)
VOLUME ["/app/data", "/app/cookies", "/app/downloads"]

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium (for HUDL auth & Trace fallback)
RUN playwright install chromium --with-deps

# Copy application source
COPY . /app

EXPOSE 8000

CMD ["uvicorn", "api_service:app", "--host", "0.0.0.0", "--port", "8000"]
