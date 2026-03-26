# ── Stage 1: Build frontend ───────────────────────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /build/frontend

# Install dependencies first for layer caching
COPY frontend/package*.json ./
RUN npm ci

# Build Tailwind + Vite bundle
COPY frontend/ ./
RUN npm run build


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# ffmpeg from Debian Bookworm includes librubberband (required for pitch shifting)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY run.py ./
COPY server/ ./server/

# Copy built frontend from Stage 1
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

# Runtime directories for media and persistent data
RUN mkdir -p /media/karaoke /data

# Defaults — override with environment variables or docker-compose
ENV SK_MEDIA_DIR=/media/karaoke \
    SK_DB_PATH=/data/superkaraoke.db \
    SK_HOST=0.0.0.0 \
    SK_PORT=8080

EXPOSE 8080

# Declare mount points so Docker knows these are external volumes
VOLUME ["/media/karaoke", "/data"]

CMD ["python", "run.py"]
