# -- Stage 1: build the React + Vite frontend -----------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
COPY plugins/ /plugins/
RUN npm run build

# -- Stage 2: build spore-nfs (Go) ------------------------------------------------------
FROM golang:1.25-alpine AS spore-nfs
WORKDIR /src
COPY spore-nfs/go.mod spore-nfs/go.sum* ./
RUN go mod download
COPY spore-nfs/main.go ./
RUN CGO_ENABLED=0 go build -o /spore-nfs .

# -- Stage 3: build spore-smb (Rust) ---------------------------------------------------
FROM rust:slim AS spore-smb
RUN apt-get update -qq && apt-get install -y -qq pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY spore-smb/ ./
RUN cargo build --release && cp target/release/spore-smb /spore-smb

# -- Stage 4: Python runtime ----------------------------------------------------------
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
COPY --from=spore-nfs /spore-nfs /usr/local/bin/spore-nfs
COPY --from=spore-smb /spore-smb /usr/local/bin/spore-smb

ENV MYCELIUM_BASE=http://127.0.0.1:8088

EXPOSE 8088 2049 445

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
port=os.environ.get('LISTEN_PORT','8088'); \
r=urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5); \
sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["sh", "-c", "LISTEN_ADDR=:2049 spore-nfs & LISTEN_ADDR=0.0.0.0:445 spore-smb & exec gunicorn --bind ${LISTEN_HOST}:${LISTEN_PORT} --workers 1 --threads 16 --access-logfile - app:app"]
