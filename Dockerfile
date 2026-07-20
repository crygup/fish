FROM python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg librsvg2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 fishie

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=fishie:fishie . .
RUN mkdir -p files/downloads \
    && chown fishie:fishie files/downloads \
    && chmod -R a=rX /app

USER fishie
EXPOSE 8001
CMD ["python", "launcher.py"]
