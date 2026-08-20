FROM python:3.12-slim

# img2pdf behöver libjpeg/zlib via Pillow-hjulen, som redan är förbyggda.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRENLY_DB=/data/prenly.db \
    PRENLY_OUTPUT=/output \
    PRENLY_TZ=Europe/Stockholm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY prenly_dl ./prenly_dl
RUN pip install --no-cache-dir ".[web]"

RUN mkdir -p /data /output /conf

VOLUME ["/data", "/output"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"

ENTRYPOINT ["prenly-dl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
