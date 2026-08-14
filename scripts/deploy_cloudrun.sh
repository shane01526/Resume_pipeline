#!/usr/bin/env bash
# Deploy to Google Cloud Run.
#
#   ./scripts/deploy_cloudrun.sh            build from source and deploy
#   ./scripts/deploy_cloudrun.sh --secrets  (re)create the secrets, then deploy
#
# Why Cloud Run over Render's free tier: the free tier there is 512MB and spins down after
# 15 minutes, and two services (web + cron) exceed the 750 free hours. Cloud Run scales to
# zero without a spin-down penalty on the request path we care about, and the scheduler
# moved to GitHub Actions.
#
# Requirements: gcloud CLI, authenticated (`gcloud auth login`), with a project selected.
set -euo pipefail

SERVICE="${SERVICE:-resume-pipeline}"
REGION="${REGION:-asia-east1}"          # Taipei — closest to you and to Notion's edge

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# Resolve the gcloud binary BEFORE anything calls it. The Windows installer does not put
# gcloud on Git Bash's PATH, so `command -v gcloud` fails on a machine where gcloud is
# installed and authenticated — and because PROJECT used to be computed by calling gcloud
# on this line, the script died with "no GCP project selected" instead of saying gcloud
# was not found. Wrong diagnosis, and the user goes off re-running `config set project`.
if command -v gcloud >/dev/null 2>&1; then
    GCLOUD=gcloud
else
    for candidate in \
        "$LOCALAPPDATA/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd" \
        "/c/Users/$USERNAME/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd" \
        "/c/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd" \
        "/c/Program Files/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
    do
        [ -f "$candidate" ] && { GCLOUD="$candidate"; break; }
    done
    [ -n "${GCLOUD:-}" ] || die "gcloud not found: https://cloud.google.com/sdk/docs/install"
    info "using gcloud at $GCLOUD"
fi

PROJECT="${PROJECT:-$("$GCLOUD" config get-value project 2>/dev/null | tr -d '\r')}"

# 2Gi, because 1Gi is not enough and this was measured the wrong way twice.
#
# A real run on Cloud Run died with "Memory limit of 1024 MiB exceeded with 1184 MiB used",
# mid-LaTeX, after both HTML PDFs had been written. Measuring the whole render in one
# container (docker stats, 2s sampling) gives a peak of 1041 MiB — over the 1Gi limit on its
# own, before Cloud Run's own overhead.
#
# The earlier "512MB is enough" note was measured from `local_run.py --render-only`, which
# renders sequentially from a fixture. It missed the thing that actually costs: the web
# service holds Playwright's Chromium resident while Tectonic runs, so the two peaks
# overlap. That is why the container passed at --memory=512m in testing and still OOMed in
# production.
#
# Cost: Cloud Run bills memory×time, and this service is idle almost always (scale to zero,
# roughly one run a week), so doubling the ceiling costs approximately nothing.
MEMORY="${MEMORY:-2Gi}"
# 1 CPU is under Cloud Run's minimum for 2Gi (it requires >= 1, and 2 for >4Gi); keeping 1
# is valid, but Tectonic and Chromium both benefit and the extra is only billed while a
# request is in flight.
CPU="${CPU:-2}"
# Rendering six artifacts takes tens of seconds; the default 300s would cut a run short.
TIMEOUT="${TIMEOUT:-900}"
# One instance at a time. Two would race on the same repository state, and the loser's
# commit would be rejected.
MAX_INSTANCES="${MAX_INSTANCES:-1}"
CONCURRENCY="${CONCURRENCY:-4}"

# --no-cpu-throttling is passed to `run deploy` below, and the whole design depends on it.
#
# POST /api/runs answers 202 immediately and does the nine stages in a FastAPI background
# task — which is correct on Render, and broken by default on Cloud Run: outside a request
# Cloud Run throttles the instance's CPU to nearly nothing, so the background work crawls
# and then dies when the idle instance is reclaimed.
#
# Observed exactly that: a run wrote its diff counts (the last step before rendering) 615
# seconds after being triggered — work that takes ~90s locally — then stopped at status
# "Building" with `error: null` and no artifacts. A null error with a non-terminal status is
# the signature of the process being killed rather than raising.
#
# The alternative is doing the work inside the request instead of after it. That would need
# the trigger to hold a connection open for the whole run, so a client timeout would abort a
# render, and Slack's 3-second ack rule makes it impossible for the /resume command path.

# BEDROCK_API_KEY is deliberately NOT in this list. It expires within 12 hours, so binding
# it as a Secret Manager version would mean a new Cloud Run revision on every rotation.
# It ships as a plain env var for the initial boot and is rotated in place afterwards via
# `scripts/set_bedrock_key.py` → POST /admin/llm-key. ANTHROPIC_API_KEY stays here for the
# LLM_PROVIDER=anthropic path, which uses a long-lived key.
SECRETS=(ANTHROPIC_API_KEY NOTION_TOKEN SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GITHUB_TOKEN APPROVAL_HMAC_SECRET TRIGGER_TOKEN)

# Read one value out of .env, in one place instead of five copies of the same pipeline.
#
# `tr -d '\r'` is belt-and-braces for the CRLF checkout on Windows. Command substitution
# already strips a trailing \r together with the newline (verified with od, after I had
# claimed otherwise), so this only matters for a value with an interior carriage return —
# which no credential has. It stays because a corrupt secret in Secret Manager is invisible:
# the dashboard shows a value that looks right and Slack just answers invalid_token.
env_value() {
    grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true
}

[ -n "$PROJECT" ] || die "no GCP project selected. Run: gcloud config set project YOUR_PROJECT"

info "project=$PROJECT service=$SERVICE region=$REGION"

# --- one-time API enablement (idempotent) -----------------------------------
info "enabling required APIs"
"$GCLOUD" services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    --project "$PROJECT" --quiet

# --- secrets ----------------------------------------------------------------
# Secret Manager rather than plain env vars: Cloud Run env vars are visible to anyone with
# viewer access to the service, and these grant push access to a repo and posting rights in
# Slack.
if [ "${1:-}" = "--secrets" ]; then
    info "creating or updating secrets — values are read from your .env"
    [ -f .env ] || die ".env not found. Copy .env.example and fill it in first."

    for name in "${SECRETS[@]}"; do
        value=$(env_value "$name")
        if [ -z "$value" ]; then
            # APPROVAL_HMAC_SECRET and TRIGGER_TOKEN can be generated; the rest cannot.
            case "$name" in
                APPROVAL_HMAC_SECRET|TRIGGER_TOKEN)
                    value=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
                    # Written back to .env, because TRIGGER_TOKEN has to be copied into a
                    # GitHub Actions secret and Secret Manager will not show it again.
                    # Generating a value the user can never read is a dead end.
                    if grep -qE "^${name}=" .env; then
                        python - "$name" "$value" <<'PY'
import sys
from pathlib import Path
name, value = sys.argv[1], sys.argv[2]
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
path.write_text(
    "\n".join(f"{name}={value}" if line.startswith(f"{name}=") else line for line in lines)
    + "\n",
    encoding="utf-8",
)
PY
                    else
                        printf '%s=%s\n' "$name" "$value" >> .env
                    fi
                    info "  $name: generated and written to .env"
                    ;;
                *)
                    printf '\033[33mskip\033[0m %s: not set in .env\n' "$name"
                    continue
                    ;;
            esac
        fi

        if "$GCLOUD" secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
            printf '%s' "$value" | "$GCLOUD" secrets versions add "$name" \
                --data-file=- --project "$PROJECT" --quiet >/dev/null
            info "  $name: new version"
        else
            printf '%s' "$value" | "$GCLOUD" secrets create "$name" \
                --data-file=- --replication-policy=automatic \
                --project "$PROJECT" --quiet >/dev/null
            info "  $name: created"
        fi
    done

    # Cloud Run's runtime service account needs read access to each secret.
    project_number=$("$GCLOUD" projects describe "$PROJECT" --format='value(projectNumber)')
    runtime_sa="${project_number}-compute@developer.gserviceaccount.com"
    info "granting secret access to $runtime_sa"
    for name in "${SECRETS[@]}"; do
        "$GCLOUD" secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 || continue
        "$GCLOUD" secrets add-iam-policy-binding "$name" \
            --member="serviceAccount:${runtime_sa}" \
            --role=roles/secretmanager.secretAccessor \
            --project "$PROJECT" --quiet >/dev/null
    done
fi

# --- build the secret and env flags -----------------------------------------
secret_flags=()
for name in "${SECRETS[@]}"; do
    if "$GCLOUD" secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
        secret_flags+=("${name}=${name}:latest")
    fi
done
[ ${#secret_flags[@]} -gt 0 ] || die "no secrets found. Run with --secrets first."

# SLACK_DM_CHANNEL is an ID (channel `C…` or user `U…`), not a credential, so it stays a
# plain env var.
slack_channel=$(env_value SLACK_DM_CHANNEL)

# Seed key for the first boot. Rotations after this go through /admin/llm-key rather than a
# redeploy — see scripts/set_bedrock_key.py.
bedrock_key=$(env_value BEDROCK_API_KEY)
[ -n "$bedrock_key" ] || bedrock_key=$(env_value AWS_BEARER_TOKEN_BEDROCK)

env_vars=(
    "STORAGE_BACKEND=github"   # REQUIRED here: Cloud Run's disk is ephemeral
    "GITHUB_REPO=shane01526/Resume_pipeline"
    "GIT_BRANCH=main"
    "RENDERERS=html,latex,docx"
    "APPROVAL_TIMEOUT_HOURS=72"
    "LLM_PROVIDER=bedrock"
    "AWS_REGION=${AWS_REGION:-us-east-1}"
    # Outside the repo on purpose: state/ and output/ are committed to a public repo.
    "BEDROCK_KEY_FILE=/tmp/bedrock_key.json"
)
[ -n "$slack_channel" ] && env_vars+=("SLACK_DM_CHANNEL=${slack_channel}")
if [ -n "$bedrock_key" ] && [ "${bedrock_key#bedrock-api-key-}" != "$bedrock_key" ]; then
    env_vars+=("BEDROCK_API_KEY=${bedrock_key}")
else
    info "no BEDROCK_API_KEY in .env — set one after deploy with scripts/set_bedrock_key.py"
fi

# `^@^` tells gcloud to split this dict on @ instead of a comma. Required, not tidiness:
# RENDERERS=html,latex,docx contains commas, so the default comma delimiter parsed `latex`
# as its own entry and the deploy aborted with
#   argument --set-env-vars: Bad syntax for dict arg: [latex]
# @ is safe as a separator because no value here can contain one — the longest is the
# base64-ish Bedrock key, whose alphabet is [A-Za-z0-9+/=-].
joined_env="^@^$(IFS=@; echo "${env_vars[*]}")"
# Secrets keep the default comma: every value is NAME=NAME:latest, which has none.
joined_secrets=$(IFS=,; echo "${secret_flags[*]}")

# --- deploy -----------------------------------------------------------------
info "deploying (first build takes ~10 minutes: Chromium and Tectonic)"
"$GCLOUD" run deploy "$SERVICE" \
    --source . \
    --project "$PROJECT" \
    --region "$REGION" \
    --platform managed \
    --memory "$MEMORY" \
    --cpu "$CPU" \
    --timeout "$TIMEOUT" \
    --max-instances "$MAX_INSTANCES" \
    --concurrency "$CONCURRENCY" \
    --no-cpu-throttling \
    --set-env-vars "$joined_env" \
    --set-secrets "$joined_secrets" \
    --allow-unauthenticated \
    --quiet

# tr -d '\r': gcloud.cmd on Windows emits CRLF, and a trailing \r inside PUBLIC_BASE_URL
# would land in every approval link in Slack.
url=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format='value(status.url)' | tr -d '\r')

# PUBLIC_BASE_URL isn't known until the service exists, so it takes a second pass. Without
# it, approval links in Slack point at localhost.
info "setting PUBLIC_BASE_URL=$url"
"$GCLOUD" run services update "$SERVICE" \
    --project "$PROJECT" --region "$REGION" \
    --update-env-vars "PUBLIC_BASE_URL=${url}" --quiet >/dev/null

# Write it into the local .env too. This is not a convenience: scripts/set_bedrock_key.py
# reads PUBLIC_BASE_URL to find the deployed service, and with it empty the script skipped
# the remote update, printed "Done", and left the deployed key untouched — the one thing it
# exists to prevent. Silent partial success is the worst failure mode for a rotation tool.
python - "$url" <<'PY'
import sys
from pathlib import Path

url = sys.argv[1]
path = Path(".env")
if not path.is_file():
    raise SystemExit(0)

lines = path.read_text(encoding="utf-8").splitlines()
line = f"PUBLIC_BASE_URL={url}"
if any(existing.startswith("PUBLIC_BASE_URL=") for existing in lines):
    lines = [line if e.startswith("PUBLIC_BASE_URL=") else e for e in lines]
else:
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"  .env: PUBLIC_BASE_URL={url}")
PY

trigger_token=$(env_value TRIGGER_TOKEN)

printf '\n\033[32mdeployed\033[0m %s\n\n' "$url"
echo "Next:"
echo "  1. curl ${url}/health      (/healthz is intercepted by Cloud Run)"
echo "     publish_ready should be true, and all three tools true."
echo
echo "  2. Slack app -> set both Request URLs:"
echo "       Slash Commands            ${url}/slack/commands"
echo "       Interactivity & Shortcuts ${url}/slack/interactions"
echo
echo "  3. GitHub -> Settings -> Secrets and variables -> Actions -> New repository secret:"
echo "       SERVICE_URL   = ${url}"
if [ -n "$trigger_token" ]; then
    echo "       TRIGGER_TOKEN = ${trigger_token}"
else
    echo "       TRIGGER_TOKEN = (read it from your .env)"
fi
echo
echo "  4. Actions -> 'Scheduled resume run' -> Run workflow, to test the trigger"
