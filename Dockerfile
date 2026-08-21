# calibredb behövs för Calibre-integrationen, men bara som klient mot en
# content server. Hela calibre är 627 MB; det som faktiskt används är 280.
# Vad som strukits är mätt, inte gissat: utan Qt, ffmpeg, onnxruntime,
# översättningar och GUI-resurser klarar calibredb fortfarande add,
# add_format, set_metadata och list mot servern.
FROM debian:bookworm-slim AS calibre

ARG CALIBRE_VERSION=9.11.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/calibre \
    && curl -fsSL "https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-x86_64.txz" \
       | tar -xJ -C /opt/calibre \
    && rm -rf /opt/calibre/translations \
              /opt/calibre/share \
              /opt/calibre/plugins \
              /opt/calibre/man-pages \
              /opt/calibre/lib/libQt6*.so.* \
              /opt/calibre/lib/python*/site-packages/PyQt6 \
              /opt/calibre/lib/libonnxruntime.so.* \
              /opt/calibre/lib/libav*.so.* \
    && /opt/calibre/calibredb --version


FROM python:3.12-slim

# img2pdf behöver libjpeg/zlib via Pillow-hjulen, som redan är förbyggda.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRENLY_DB=/data/prenly.db \
    PRENLY_OUTPUT=/output \
    PRENLY_TZ=Europe/Stockholm \
    PATH="/opt/calibre:${PATH}"

# calibredb länkar mot systemets libgl och libxcb även utan Qt-lagret.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libxcb1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=calibre /opt/calibre /opt/calibre

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
