FROM python:3.11-slim
 
# Install system dependencies + deno (required by yt-dlp for YouTube JS extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
&& curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
&& rm -rf /var/lib/apt/lists/*
 
WORKDIR /app
 
# Create cookies mount point
RUN mkdir -p /app/cookies
 
# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# Install Playwright + Chromium (for HUDL auth & Trace fallback)
RUN playwright install chromium --with-deps
 
# Copy application source
COPY . /app
 
EXPOSE 8000
 
CMD ["uvicorn", "api_service:app", "--host", "0.0.0.0", "--port", "8000"]