FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium (for HUDL auth & Trace fallback)
RUN playwright install chromium --with-deps

COPY . /app

EXPOSE 8000

CMD ["uvicorn", "api_service:app", "--host", "0.0.0.0", "--port", "8000"]
