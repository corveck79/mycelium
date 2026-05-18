FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8088

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"LISTEN_PORT\",\"8088\")}/health').read(); sys.exit(0)" || exit 1

CMD ["sh", "-c", "gunicorn --bind ${LISTEN_HOST}:${LISTEN_PORT} --workers 2 --threads 4 --access-logfile - app:app"]
