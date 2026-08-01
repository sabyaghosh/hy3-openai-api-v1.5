FROM python:3.11.9-slim-bookworm

# Metadata
LABEL org.opencontainers.image.title="hy3-openai-api" \
      org.opencontainers.image.description="OpenAI-compatible API proxy for Tencent Hy3 295B MoE via HuggingFace Gradio API" \
      org.opencontainers.image.source="https://github.com/sabyaghosh/hy3-openai-api" \
      org.opencontainers.image.licenses="MIT"

# Non-root user for security
RUN useradd --create-home --uid 1000 app
WORKDIR /home/app

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy app code (logging_layer.py is imported by server.py — must be included).
# Docs are included for reference; they don't affect runtime but are useful
# when exec-ing into the container.
COPY --chown=app:app server.py logging_layer.py hy3.sh tools_example.json README.md CHANGELOG.md DEPLOY.md LICENSE ./
RUN chmod +x hy3.sh

USER app
EXPOSE 8000

# Render injects PORT env var. Default to 8000 for local runs.
# PYTHONUNBUFFERED=1 ensures logs appear immediately (not block-buffered).
ENV PORT=8000 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# Add HEALTHCHECK so standalone Docker runs report health status.
# render.yaml uses healthCheckPath separately; this is for `docker run` users.
# NOTE: HEALTHCHECK CMD does not expand ${PORT} unless wrapped in sh -c.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c 'python -c "import os,urllib.request,sys; r=urllib.request.urlopen(\"http://localhost:\"+os.environ.get(\"PORT\",\"8000\")+\"/health\",timeout=3); sys.exit(0 if r.status==200 else 1)" || exit 1'

# --proxy-headers: trust X-Forwarded-For/X-Real-IP from the load balancer so
# request.client.host reflects the real client IP (not the proxy's).
# --forwarded-allow-ips='*': accept forwarded headers from any upstream proxy
# (safe behind Render/Fly/Cloud Run/Koyeb's managed load balancers).
CMD ["sh", "-c", "uvicorn server:app --host ${HOST} --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
