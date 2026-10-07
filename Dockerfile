# Pinned Python and Debian release: every rebuild gets the same base. Bump it on purpose
# (see "Updating dependencies" in the README).
FROM python:3.11.17-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the runtime modules (see .dockerignore). They stay owned by root:
# the tracker can read its code, not change it.
COPY . .

RUN useradd --system --no-create-home --shell /usr/sbin/nologin app
USER app

# Unhealthy once no hourly check has reached Odoo for too long (heartbeat.py)
HEALTHCHECK --interval=5m --timeout=10s --start-period=15m --retries=3 \
    CMD ["python", "heartbeat.py"]

CMD ["python", "-u", "automated_tracker.py"]
