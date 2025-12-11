# Use Python with Playwright 1.57.0 pre-installed
FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binaries explicitly (ensures they exist in container)
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV BROWSER_HEADLESS=true

# Default port (Railway overrides this with its own PORT env var)
ENV PORT=8080

# Expose the port Railway uses
EXPOSE 8080

# Simple shell CMD that Railway can reliably execute
# Using exec form with sh -c for proper $PORT expansion
CMD sh -c "echo '[STARTUP] Launching uvicorn on port $PORT' && exec uvicorn services.api.app:app --host 0.0.0.0 --port ${PORT:-8080}"
