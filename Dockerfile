# Resume pipeline image: Python + Chromium + Tectonic + poppler + CJK fonts.
#
# Two-stage build. The heavy, rarely-changing pieces (Tectonic binary, its package cache)
# are resolved in the builder so an application-code change doesn't re-download them.
#
# Size note: Tectonic is deliberate. A full `texlive-xetex + latex-extra` install pushes
# this image past 2.5 GB and adds ~15 minutes to every build; Tectonic is one ~50 MB
# binary on the same XeTeX engine, fetching only the packages a document actually uses.

# --- builder ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ARG TECTONIC_VERSION=0.15.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        # Tectonic links against these at runtime; needed here to warm the cache.
        libfontconfig1 \
        libgraphite2-3 \
        libharfbuzz0b \
        libicu72 \
        libssl3 \
        # Noto CJK must be present while warming, or the warm-up document fails to
        # resolve its fonts and the cache ends up incomplete.
        fonts-noto-cjk \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
      "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
      | tar -xz -C /usr/local/bin \
    && chmod +x /usr/local/bin/tectonic \
    && tectonic --version

# Warm the package cache at build time. Without this the first real render pays a
# multi-second download for fontspec, xeCJK, geometry, and friends — and fails outright
# if the container has no egress.
COPY docker/warmup.tex /tmp/warmup/warmup.tex
RUN cd /tmp/warmup \
    && TECTONIC_CACHE_DIR=/opt/tectonic-cache tectonic --chatter minimal warmup.tex \
    && rm -rf /tmp/warmup

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Tectonic reads its cache from here; baked in the builder above.
    TECTONIC_CACHE_DIR=/opt/tectonic-cache \
    # Playwright's default is a home-relative path, which differs between the build
    # user and the runtime user. Pinning it avoids a re-download at first launch.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        # pdftoppm, for page images on the diff page.
        poppler-utils \
        # publish stage commits artifacts back to the repo.
        git \
        # Tectonic runtime libs.
        libfontconfig1 \
        libgraphite2-3 \
        libharfbuzz0b \
        libicu72 \
        libssl3 \
        # Fonts. Noto CJK carries both scripts, so a stray English word inside Chinese
        # text keeps one consistent face instead of falling back.
        fonts-noto-cjk \
        fonts-noto-cjk-extra \
        fonts-noto-core \
        fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

COPY --from=builder /usr/local/bin/tectonic /usr/local/bin/tectonic
COPY --from=builder /opt/tectonic-cache /opt/tectonic-cache

WORKDIR /app

# Dependencies before source, so editing a .py file doesn't reinstall the world.
#
# `pip install .` cannot be used at this point: setuptools reads the package list from
# pyproject.toml and fails with "package directory 'pipeline' does not exist" when the
# source isn't there yet. requirements.txt carries the same pins and is checked against
# pyproject.toml by tests/test_packaging.py, so the two cannot drift.
# pyproject.toml is copied alongside so the editable install below can read it; it is
# metadata only, so touching it (rather than a .py file) is the only thing that busts this
# layer.
COPY requirements.txt pyproject.toml ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Chromium plus the system libraries it needs. `--with-deps` resolves those from the
# distro rather than us guessing at the list.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pipeline/ ./pipeline/
COPY web/ ./web/
COPY templates/ ./templates/
COPY scripts/ ./scripts/
COPY docker/ ./docker/

# Now that the source is present, install the package itself so `import pipeline` works
# from any working directory. --no-deps because the dependencies are already resolved above.
RUN pip install --no-deps -e .

RUN mkdir -p state/runs output/en output/zh sources \
    && chmod +x docker/entrypoint.sh

# Non-root. Chromium is the reason this matters — a browser running as root in a
# container is a needless escalation path, and Playwright warns about it.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app /opt/playwright
USER app

EXPOSE 8000

# The entrypoint makes /app a git checkout before anything starts. git IS the database
# here (see pipeline/state.py), and the image ships code but not the repository — without
# this, `git add` in publish.py fails on a directory with no .git, right after a run has
# been rendered and approved.
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Render sets PORT; the default keeps `docker run -p 8000:8000` working locally.
CMD ["sh", "-c", "uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
