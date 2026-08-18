# Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits don't reinstall the world.
COPY packages/core/pyproject.toml /app/packages/core/pyproject.toml
COPY packages/core/juicebox/__init__.py /app/packages/core/juicebox/__init__.py
RUN pip install --upgrade pip && pip install "/app/packages/core[postgres]"

COPY packages/core /app/packages/core
COPY eval /app/eval
RUN pip install --no-deps -e /app/packages/core

RUN useradd --create-home --uid 10001 juicebox && chown -R juicebox /app
USER juicebox

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "juicebox.main:app", "--host", "0.0.0.0", "--port", "8000"]
