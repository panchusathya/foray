# Build context is packages/foray (self-contained service).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache

# psql is required by scripts/migrate.sh at container start.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch first: the default PyPI wheel drags in multi-GB CUDA libraries
# this service never uses. Must precede the package install or pip would
# resolve torch from PyPI as a sentence-transformers dependency.
RUN pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu

# Dependency layer first so source edits don't reinstall the world.
COPY pyproject.toml /app/pyproject.toml
COPY foray/__init__.py /app/foray/__init__.py
RUN pip install /app

# Bake the embedding model into the image: the query path embeds locally and
# must not depend on Hugging Face being reachable at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

COPY . /app
RUN pip install --no-deps -e /app

RUN useradd --create-home --uid 10002 foray && chown -R foray /app /opt/hf-cache
USER foray

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=8).status==200 else 1)"

CMD ["uvicorn", "foray.api.app:app", "--host", "0.0.0.0", "--port", "8100"]
