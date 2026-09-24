# ═══════════════════════════════════════════════════════════════════
# SmartPDF OCR — Multi-Stage Production Dockerfile
# Stage 1: Build React 19 Frontend
# Stage 2: Python 3.12 Backend with Tesseract OCR (vie+eng) & PyMuPDF
# ═══════════════════════════════════════════════════════════════════

FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
RUN corepack enable && corepack prepare pnpm@latest --activate

COPY frontend/package.json frontend/pnpm-lock.yaml* frontend/pnpm-workspace.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install

COPY frontend ./
RUN pnpm run build

# Stage 2: Runtime
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend \
    PORT=8788 \
    DATABASE_PATH=/data/ocr_history.db \
    UPLOAD_DIR=/data/uploads \
    TESSERACT_LANG=eng+vie

WORKDIR /app/backend

# Install system dependencies: Tesseract OCR (vie + eng), poppler-utils, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-vie \
    poppler-utils \
    fonts-dejavu-core \
    fonts-liberation \
    libgl1 \
    libglib2.0-0 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy Backend code
COPY backend/ /app/backend/

# Copy compiled Frontend static assets
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# Create persistent directories and non-root user
RUN mkdir -p /data/uploads /data/backups && \
    useradd -u 1000 -m smartpdf && \
    chown -R smartpdf:smartpdf /app /data

USER smartpdf

EXPOSE 8788

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf http://127.0.0.1:${PORT}/api/health || exit 1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
