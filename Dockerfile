FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create a non-root user
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Health check
HEALTHCHECK --interval=5m --timeout=30s --start-period=5s --retries=3 \
    CMD python -c "import requests; print('Health check passed')" || exit 1

# Set Python unbuffered for immediate output
ENV PYTHONUNBUFFERED=1

# Run the automated tracker
CMD ["python", "-u", "automated_tracker.py"]