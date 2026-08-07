#!/bin/sh
# Container entrypoint: make /app a working git checkout, then start the service.
#
# This exists because git IS the database (see pipeline/state.py). The image ships the
# code but not the repository, so without this step /app has no .git and the first
# publish fails at `git add` — after a run has already rendered and been approved, which
# is the worst possible moment to discover it.
#
# Two paths:
#   - No .git yet (fresh container): clone the repo over /app, preserving the image's code.
#   - Already a checkout (a restart with a persistent disk): just fetch and reset.
#
# Both are idempotent, so a container restart is always safe.
set -eu

REPO_DIR=/app
BRANCH="${GIT_BRANCH:-main}"

log() { printf '%s entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPO:-}" ]; then
    # Not fatal: the diff page, /healthz, and rendering all work without git. Publishing
    # is what breaks, and /healthz already reports that.
    log "GITHUB_TOKEN or GITHUB_REPO unset — skipping checkout; publishing will be unavailable"
else
    REMOTE="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git"

    if [ -d "$REPO_DIR/.git" ]; then
        log "existing checkout found; fetching origin/$BRANCH"
        git -C "$REPO_DIR" remote set-url origin "$REMOTE"
        git -C "$REPO_DIR" fetch --depth 1 origin "$BRANCH"
        # Hard reset rather than pull: the container is a consumer of repo state, never the
        # place where local edits should survive.
        git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
    else
        log "cloning $GITHUB_REPO into $REPO_DIR"
        # Clone into a temp dir, then move .git into place. Cloning directly over a
        # non-empty /app is refused by git, and the image's code must not be replaced —
        # the deployed image is the source of truth for code, the repo for state.
        TMP_CLONE=$(mktemp -d)
        git clone --depth 1 --branch "$BRANCH" "$REMOTE" "$TMP_CLONE/repo"
        mv "$TMP_CLONE/repo/.git" "$REPO_DIR/.git"
        rm -rf "$TMP_CLONE"

        # The working tree now differs from HEAD (image code vs cloned code). Checkout the
        # state and output directories only, so state/ and output/ reflect the repo while
        # the code stays as built.
        git -C "$REPO_DIR" checkout -- state output 2>/dev/null || true
        log "checkout ready at $(git -C "$REPO_DIR" rev-parse --short HEAD)"
    fi

    # Required for git to operate on a directory it doesn't consider owned by this user.
    git config --global --add safe.directory "$REPO_DIR"
    git config --global user.name "${GIT_AUTHOR_NAME:-resume-pipeline}"
    git config --global user.email "${GIT_AUTHOR_EMAIL:-bot@resume-pipeline.local}"
fi

# Directories the pipeline writes to, in case the repo didn't carry them.
mkdir -p "$REPO_DIR/state/runs" "$REPO_DIR/output/en" "$REPO_DIR/output/zh" "$REPO_DIR/sources"

log "starting: $*"
exec "$@"
