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

# The free LinkedIn scrape, off by default.
#
# Build with `--build-arg INSTALL_PLAYWRIGHT=true` to turn it on. It is not on
# by default for two reasons: it adds Chromium and its apt dependencies to an
# image that otherwise needs no apt at all (measured on prod: 526MB -> 2.22GB,
# i.e. +1.7GB, on a host at 71% disk), and it is useless without
# LINKEDIN_SESSION_COOKIE -- LinkedIn serves
# an auth wall to a browser that is not logged in, so an installed-but-
# cookieless scraper fails on every profile and falls through to the paid
# provider anyway, just more slowly.
#
# `enrich.profile_sources` already tries the scrape first and pays only on
# failure, so turning this on is the only step needed once a cookie exists.
ARG INSTALL_PLAYWRIGHT=false
# Browsers go to a shared path, not $HOME/.cache. `playwright install` runs as
# root here, but every service in this image runs as uid 10001 -- so the
# default cache location puts Chromium in /root/.cache, where the app cannot
# see it. The image builds clean and the failure only shows up at launch, as
# "Executable doesn't exist ... run playwright install". This env var is read
# at install time *and* at launch time, which is what makes the two agree.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then       pip install "playwright>=1.40" &&       playwright install --with-deps chromium &&       chmod -R a+rX /ms-playwright;     fi

# Includes migrations/, which `juicebox migrate` resolves relative to the package.
COPY packages/core /app/packages/core
COPY eval /app/eval
RUN pip install --no-deps -e /app/packages/core

# `data/files` is created here, before the volume is mounted over it, because
# Docker seeds a *new* named volume from the image directory — ownership
# included. Without this line the mount point is created by the daemon as
# root:root, the app runs as uid 10001, and every package build dies on
# "[Errno 13] Permission denied: /app/data/files/org_...".
#
# An existing volume is not re-seeded, so a stack that already ran needs the
# ownership fixed once by hand — see deploy/README.md.
RUN useradd --create-home --uid 10001 juicebox \
 && mkdir -p /app/data/files \
 && chown -R juicebox /app
USER juicebox

EXPOSE 8000

# Assumes the default CMD below. Any service that reuses this image with a
# non-HTTP command (the worker runs `juicebox.cli work`) must override this with
# `healthcheck: {disable: true}` in compose, or it reports unhealthy forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "juicebox.main:app", "--host", "0.0.0.0", "--port", "8000"]
