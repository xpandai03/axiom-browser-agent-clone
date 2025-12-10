# Use Python with Playwright pre-installed
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Railway provides PORT env variable
# Default to 8000 if not set
ENV PORT=8000

# Expose port (Railway overrides this)
EXPOSE 8000

# Use shell form to allow $PORT expansion
CMD uvicorn services.api.app:app --host 0.0.0.0 --port $PORT
