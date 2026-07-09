# -- Stage 1: build the React + Vite frontend -----------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
COPY plugins/ /plugins/
RUN npm run build

# -- Stage 2: build spore-9p (Go) ------------------------------------------------------
FROM golang:1.25-alpine AS spore-9p
WORKDIR /src
COPY spore-9p/go.mod spore-9p/go.sum* ./
RUN go mod download
COPY spore-9p/main.go ./
RUN CGO_ENABLED=0 go build -o /spore-9p .

# -- Stage 3: Python runtime ----------------------------------------------------------
FROM python:3.12-slim

ARG BUILD_VERSION=dev
LABEL org.opencontainers.image.title="mycelium" \
      org.opencontainers.image.description="Self-hosted media pipeline: watchlist to .strm via TorBox" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/corveck79/mycelium"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8088 \
    LIBVA_DRIVER_NAME=iHD

WORKDIR /app

ARG TARGETARCH
# Add non-free repo for Intel VA-API driver (iHD = Gen8+, includes J3455/J4125)
# intel-media-va-driver is x86-only; skip on arm64
RUN echo "deb http://deb.debian.org/debian bookworm contrib non-free non-free-firmware" \
        > /etc/apt/sources.list.d/non-free.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libva2 \
        libva-drm2 \
    && if [ "$TARGETARCH" = "amd64" ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY releases.json ./
COPY plugins/ ./plugins/
COPY templates/ ./templates/
COPY docs/ ./docs/
# Built SPA from stage 1 (Vite writes to ../static/app relative to frontend/)
COPY --from=frontend /static/app/ ./static/app/
# Also copy pre-built SPA if present (skips npm build when static/app/ is tracked)
COPY static/ ./static/
COPY --from=spore-9p /spore-9p /usr/local/bin/spore-9p

ENV MYCELIUM_BASE=http://127.0.0.1:8088 \
    LISTEN_ADDR=0.0.0.0:5640

EXPOSE 8088 5640

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
port=os.environ.get('LISTEN_PORT','8088'); \
r=urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5); \
sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["sh", "-c", "spore-9p & exec gunicorn --bind ${LISTEN_HOST}:${LISTEN_PORT} --workers 1 --threads 8 --access-logfile - app:app"]
