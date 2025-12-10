# Use Python with Playwright 1.57.0 pre-installed
FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binaries explicitly
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV BROWSER_HEADLESS=true

# Railway provides PORT env variable
ENV PORT=8000

EXPOSE 8000

CMD uvicorn services.api.app:app --host 0.0.0.0 --port $PORT
