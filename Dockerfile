FROM python:3.11.9-slim-bookworm

# Metadata
LABEL org.opencontainers.image.title="hy3-openai-api" \
      org.opencontainers.image.description="OpenAI-compatible API proxy for Tencent Hy3 295B MoE via HuggingFace Gradio API" \
      org.opencontainers.image.source="https://github.com/sabyaghosh/hy3-client" \
      org.opencontainers.image.licenses="MIT"

# Non-root user for security
RUN useradd --create-home --uid 1000 app
WORKDIR /home/app

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy app code (NOTE: logging_layer.py is imported by server.py — must be included)
COPY --chown=app:app server.py logging_layer.py hy3.sh tools_example.json README.md ./
RUN chmod +x hy3.sh

USER app
EXPOSE 8000

# Render injects PORT env var. Default to 8000 for local runs.
ENV PORT=8000 \
    HOST=0.0.0.0

CMD ["sh", "-c", "uvicorn server:app --host ${HOST} --port ${PORT}"]
