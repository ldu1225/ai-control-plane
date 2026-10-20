# AI Control Plane container image.
#
# Build: docker build -t ai-control-plane:2.0.0 .
# Run:   docker run --rm -p 8080:8080 -e PROJECT_ID=my-project -e AUTH_MODE=local ai-control-plane:2.0.0
#
# Configuration is read from /app/config.yaml (placeholders) and overridden with environment
# variables (PROJECT_ID, AUDIT_DATASET_ID, BILLING_*, AUTH_MODE, ADMIN_EMAILS, SETTINGS_BUCKET, ...).
# Cloud Run injects PORT; the server binds HOST:PORT.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Unprivileged runtime user. Application files stay root-owned (read-only for the app);
# only /app/data (file-based settings store when not on Cloud Run) is writable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data \
    && chmod 0700 /app/data

COPY config.yaml /app/config.yaml
COPY src/ /app/src/

USER 10001:10001

EXPOSE 8080

CMD ["python3", "-u", "src/backend_server.py"]
