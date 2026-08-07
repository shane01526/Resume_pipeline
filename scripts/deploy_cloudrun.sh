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
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

# 512MB is enough: measured peak is 54MB Python + 488MB in children (Tectonic is the
# heavy one). 1Gi leaves headroom for a longer resume without changing the free-tier maths.
MEMORY="${MEMORY:-1Gi}"
CPU="${CPU:-1}"
# Rendering six artifacts takes tens of seconds; the default 300s would cut a run short.
TIMEOUT="${TIMEOUT:-900}"
# One instance at a time. Two would race on the same repository state, and the loser's
# commit would be rejected.
MAX_INSTANCES="${MAX_INSTANCES:-1}"
CONCURRENCY="${CONCURRENCY:-4}"

SECRETS=(ANTHROPIC_API_KEY NOTION_TOKEN SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GITHUB_TOKEN APPROVAL_HMAC_SECRET TRIGGER_TOKEN)

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -n "$PROJECT" ] || die "no GCP project selected. Run: gcloud config set project YOUR_PROJECT"

command -v gcloud >/dev/null || die "gcloud not found: https://cloud.google.com/sdk/docs/install"

info "project=$PROJECT service=$SERVICE region=$REGION"

# --- one-time API enablement (idempotent) -----------------------------------
info "enabling required APIs"
gcloud services enable \
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
        value=$(grep -E "^${name}=" .env | head -1 | cut -d= -f2- || true)
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

        if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
            printf '%s' "$value" | gcloud secrets versions add "$name" \
                --data-file=- --project "$PROJECT" --quiet >/dev/null
            info "  $name: new version"
        else
            printf '%s' "$value" | gcloud secrets create "$name" \
                --data-file=- --replication-policy=automatic \
                --project "$PROJECT" --quiet >/dev/null
            info "  $name: created"
        fi
    done

    # Cloud Run's runtime service account needs read access to each secret.
    project_number=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
    runtime_sa="${project_number}-compute@developer.gserviceaccount.com"
    info "granting secret access to $runtime_sa"
    for name in "${SECRETS[@]}"; do
        gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 || continue
        gcloud secrets add-iam-policy-binding "$name" \
            --member="serviceAccount:${runtime_sa}" \
            --role=roles/secretmanager.secretAccessor \
            --project "$PROJECT" --quiet >/dev/null
    done
fi

# --- build the secret and env flags -----------------------------------------
secret_flags=()
for name in "${SECRETS[@]}"; do
    if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
        secret_flags+=("${name}=${name}:latest")
    fi
done
[ ${#secret_flags[@]} -gt 0 ] || die "no secrets found. Run with --secrets first."

# SLACK_DM_CHANNEL is a user ID, not a credential, so it stays a plain env var.
slack_channel=$(grep -E '^SLACK_DM_CHANNEL=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)

env_vars=(
    "STORAGE_BACKEND=github"   # REQUIRED here: Cloud Run's disk is ephemeral
    "GITHUB_REPO=shane01526/Resume_pipeline"
    "GIT_BRANCH=main"
    "RENDERERS=html,latex,docx"
    "APPROVAL_TIMEOUT_HOURS=72"
)
[ -n "$slack_channel" ] && env_vars+=("SLACK_DM_CHANNEL=${slack_channel}")

joined_env=$(IFS=,; echo "${env_vars[*]}")
joined_secrets=$(IFS=,; echo "${secret_flags[*]}")

# --- deploy -----------------------------------------------------------------
info "deploying (first build takes ~10 minutes: Chromium and Tectonic)"
gcloud run deploy "$SERVICE" \
    --source . \
    --project "$PROJECT" \
    --region "$REGION" \
    --platform managed \
    --memory "$MEMORY" \
    --cpu "$CPU" \
    --timeout "$TIMEOUT" \
    --max-instances "$MAX_INSTANCES" \
    --concurrency "$CONCURRENCY" \
    --set-env-vars "$joined_env" \
    --set-secrets "$joined_secrets" \
    --allow-unauthenticated \
    --quiet

url=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format='value(status.url)')

# PUBLIC_BASE_URL isn't known until the service exists, so it takes a second pass. Without
# it, approval links in Slack point at localhost.
info "setting PUBLIC_BASE_URL=$url"
gcloud run services update "$SERVICE" \
    --project "$PROJECT" --region "$REGION" \
    --update-env-vars "PUBLIC_BASE_URL=${url}" --quiet >/dev/null

trigger_token=$(grep -E '^TRIGGER_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)

printf '\n\033[32mdeployed\033[0m %s\n\n' "$url"
echo "Next:"
echo "  1. curl ${url}/healthz"
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
