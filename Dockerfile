# hictx API image (Django + DRF served by gunicorn).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# git is a runtime dependency: the app shells out to it to clone repos and blame.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev \
    && uv pip install gunicorn

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    GIT_TERMINAL_PROMPT=0 \
    DJANGO_SETTINGS_MODULE=hictx.settings

EXPOSE 8000

# 3 sync workers; the app also spawns background threads for ingestion, so keep a
# generous timeout. Scale OUT with the HPA, not up with worker count.
CMD ["uv", "run", "gunicorn", "hictx.wsgi:application", \
     "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", \
     "--timeout", "180", "--access-logfile", "-"]
