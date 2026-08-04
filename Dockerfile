FROM python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    FISHIE_EFFECT_FONT_ROOT=/opt/fishie/fonts/text

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
        fonts-noto-core \
        fonts-noto-mono \
        librsvg2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 fishie

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/fetch_media_fonts.py /tmp/fetch_media_fonts.py
RUN python /tmp/fetch_media_fonts.py \
        --destination "${FISHIE_EFFECT_FONT_ROOT}" \
    && chmod -R a=rX /opt/fishie/fonts \
    && rm /tmp/fetch_media_fonts.py

COPY --chown=fishie:fishie . .
RUN mkdir -p src/files/downloads \
    && chown fishie:fishie src/files/downloads \
    && chmod -R a=rX /app

WORKDIR /app/src
USER fishie
EXPOSE 8001
CMD ["python", "launcher.py"]
