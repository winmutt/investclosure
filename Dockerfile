FROM python:3.12-slim

# No interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# App directory
WORKDIR /app

# System deps for Chromium (required by Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps (install playwright, then download Chromium)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium && \
    playwright install-deps chromium

# Application code
COPY scraper/ ./scraper/

# Create data directories
RUN mkdir -p /app/data /app/data/backups /app/data/logs

ENV PYTHONPATH=/app

# Configurable port for health check / HTTP endpoints (default 5001)
ENV FLASK_PORT=5001

EXPOSE 5001

CMD ["python", "-m", "scraper"]
