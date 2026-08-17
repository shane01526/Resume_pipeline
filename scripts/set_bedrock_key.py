"""Install a new Bedrock short-term API key, locally and/or on the deployed service.

    python scripts/set_bedrock_key.py ABSK...              # local + deployed
    python scripts/set_bedrock_key.py ABSK... --local      # local only
    python scripts/set_bedrock_key.py --remote-only ABSK...
    python scripts/set_bedrock_key.py --status             # what is loaded where

Reads the key from a `--` argument, stdin (`... | python scripts/set_bedrock_key.py -`),
or an interactive hidden prompt if neither is given. The prompt is the default because a
key passed as an argument lands in your shell history.

Short-term Bedrock keys expire within 12 hours, so this is a routine command, not a
one-off. It updates the running service over HTTP rather than triggering a redeploy —
a Cloud Run revision takes minutes and the key would expire again the same day.

One rotation writes the key to **four** places, because each covers a different lifetime:

    in-process override   this shell / this container, immediately
    /tmp cache file       survives a restart of the *same* instance
    .env                  what the next `deploy_cloudrun.sh` seeds
    Secret Manager        what every *future* Cloud Run instance reads at startup

The last one is what makes rotation durable. `/admin/llm-key` only reaches the instance
that receives the request, so after Cloud Run scaled to zero the next instance fell back
to the deploy-time value and a run failed with "Signature expired" hours after a
successful rotation. Adding a Secret Manager version fixes that without a new revision:
`--set-secrets NAME=NAME:latest` is resolved per instance start, verified on the deployed
service (same revision, fresh instance, new value).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from pipeline.config import get_settings  # noqa: E402
from pipeline.llm_key import KEY_PREFIX, KEY_PREFIXES, LLMKeyError, set_key, status  # noqa: E402


def read_key(raw: str | None) -> str:
    """The key from an argument, stdin, or a hidden prompt."""
    if raw == "-":
        return sys.stdin.read().strip()
    if raw:
        return raw.strip()
    # No echo — this is a credential.
    return getpass.getpass(f"Bedrock API key ({KEY_PREFIX}...): ").strip()


def write_dotenv(key: str) -> bool:
    """Update BEDROCK_API_KEY in `.env`. Returns whether the file was changed.

    Not cosmetic. `.env` is what `deploy_cloudrun.sh` seeds the container's env var from, so
    a stale value there means every future deploy ships a dead key — while local runs keep
    working from the shell's AWS_BEARER_TOKEN_BEDROCK or the /tmp cache. That divergence
    caused two wrong diagnoses in one day: the deployed service returned
    "Signature expired: 20260813T143251Z" from the key in `.env` while the same command
    succeeded locally against a newer key the shell happened to hold.

    Rotating now updates all three places a key can live: this process, the cache file, and
    the deploy seed.
    """
    path = Path(".env")
    if not path.is_file():
        print("local: no .env found - skipping the deploy seed")
        return False

    lines = path.read_text(encoding="utf-8").splitlines()
    line = f"BEDROCK_API_KEY={key}"
    if any(existing.startswith("BEDROCK_API_KEY=") for existing in lines):
        if line in lines:
            return False
        lines = [line if e.startswith("BEDROCK_API_KEY=") else e for e in lines]
    else:
        lines.append(line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _warn_on_divergence() -> None:
    """Say so when the three places a key can live disagree.

    A key can sit in `.env` (the deploy seed), in the shell as AWS_BEARER_TOKEN_BEDROCK, and
    in the cache file — and the resolution order differs from what deployment uses. When they
    diverge you get the worst possible symptom: local calls succeed while the deployed
    service returns "Signature expired", and both report the same last four characters
    because the suffix is not distinctive. Printing lengths makes them distinguishable.
    """
    import os

    from pipeline.llm_key import current_key

    settings = get_settings()
    resolved = current_key(settings)
    values = {
        "in effect": resolved.key if resolved else "",
        "shell AWS_BEARER_TOKEN_BEDROCK": os.environ.get("AWS_BEARER_TOKEN_BEDROCK", ""),
        ".env BEDROCK_API_KEY": _dotenv_key(),
    }
    present = {name: value for name, value in values.items() if value}
    if len({value for value in present.values()}) <= 1:
        return

    print("\n  WARNING: these do not all hold the same key")
    for name, value in present.items():
        print(f"    {name:32} ...{value[-6:]}  ({len(value)} chars)")
    print(
        "  `.env` is what the next deploy seeds into the container, so a stale value there\n"
        "  means local runs keep working while the deployed service gets a dead key.\n"
        "  Fix: python scripts/set_bedrock_key.py <the-key-you-want>"
    )


def _dotenv_key() -> str:
    path = Path(".env")
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("BEDROCK_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


SECRET_NAME = "BEDROCK_API_KEY"

#: Where the Windows installer puts gcloud. Git Bash's PATH does not include it, so
#: `shutil.which("gcloud")` fails on a machine where gcloud is installed and authenticated.
#: Same list as scripts/deploy_cloudrun.sh — keep them in step.
_GCLOUD_CANDIDATES = (
    r"{LOCALAPPDATA}\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
)


def _gcloud() -> str | None:
    """Path to the gcloud CLI, or None."""
    import shutil

    if found := shutil.which("gcloud"):
        return found
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    for candidate in _GCLOUD_CANDIDATES:
        path = Path(candidate.format(LOCALAPPDATA=local_appdata))
        if path.is_file():
            return str(path)
    return None


def push_secret_manager(key: str) -> bool:
    """Add a new Secret Manager version, so future Cloud Run instances get this key.

    This is the difference between a rotation that lasts and one that silently reverts:
    Cloud Run resolves `NAME:latest` when an instance starts, so without a new version the
    next cold start reads whatever was current at deploy time.

    Failure is reported loudly rather than skipped. A silent skip here reproduces the exact
    trap that an empty PUBLIC_BASE_URL caused — a command that prints success while leaving
    the deployment on a dead key.
    """
    gcloud = _gcloud()
    if gcloud is None:
        print(
            "  WARNING: gcloud not found, so Secret Manager was NOT updated.\n"
            "    Future Cloud Run instances will keep reading the OLD key after a cold start.\n"
            "    Install the SDK, or pass --no-secret-manager if this machine is local-only."
        )
        return False

    project = subprocess.run(  # noqa: S603 - resolved absolute path, no shell
        [gcloud, "config", "get-value", "project"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not project or project == "(unset)":
        print(
            "  WARNING: no gcloud project selected, so Secret Manager was NOT updated.\n"
            "    Run: gcloud config set project YOUR_PROJECT"
        )
        return False

    # The key goes in on stdin, never as an argv element: arguments are visible in the
    # process table to any other user on the machine.
    result = subprocess.run(  # noqa: S603
        [gcloud, "secrets", "versions", "add", SECRET_NAME,
         "--data-file=-", "--project", project, "--quiet"],
        input=key.encode(),
        capture_output=True,
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit {result.returncode}"
        if "NOT_FOUND" in detail:
            print(
                f"  WARNING: the secret {SECRET_NAME} does not exist yet in {project}.\n"
                "    Create it once with: bash scripts/deploy_cloudrun.sh --secrets"
            )
        else:
            print(f"  WARNING: Secret Manager was NOT updated: {detail}")
        return False

    print(f"  OK Secret Manager: new version of {SECRET_NAME} in {project}")
    return True


def push_remote(key: str, base_url: str, token: str, expires_at: datetime | None) -> bool:
    """Send the key to the deployed service. Returns True on success."""
    url = f"{base_url}/admin/llm-key"
    payload: dict[str, object] = {"key": key}
    if expires_at:
        payload["expires_at"] = expires_at.isoformat()

    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-Trigger-Token": token},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"  ERROR: could not reach {url}: {exc}")
        return False

    if response.status_code == 401:
        print("  ERROR: rejected (401) - TRIGGER_TOKEN does not match the deployed service")
        return False
    if response.status_code == 404:
        print(f"  ERROR: {url} not found - the deployed revision predates this endpoint")
        return False
    if response.status_code >= 400:
        print(f"  ERROR: HTTP {response.status_code}: {response.text[:200]}")
        return False

    body = response.json()
    print(f"  OK deployed service updated (...{body.get('suffix', '????')})")
    return True


def show_status(settings: object, base_url: str, token: str) -> int:
    print("local:")
    print(f"  {json.dumps(status(settings), indent=2, default=str)}")  # type: ignore[arg-type]
    _warn_on_divergence()

    if not base_url or "localhost" in base_url:
        print("\nremote: PUBLIC_BASE_URL is unset or local - skipping")
        return 0

    print(f"\nremote ({base_url}):")
    try:
        response = httpx.get(
            f"{base_url}/admin/llm-key", headers={"X-Trigger-Token": token}, timeout=30.0
        )
        if response.status_code >= 400:
            print(f"  HTTP {response.status_code}: {response.text[:200]}")
            return 1
        print(f"  {json.dumps(response.json(), indent=2)}")
    except httpx.HTTPError as exc:
        print(f"  unreachable: {exc}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "key",
        nargs="?",
        help=f"the key ({KEY_PREFIX}...), or '-' to read stdin. Omit for a hidden prompt.",
    )
    parser.add_argument("--local", action="store_true", help="update this machine only")
    parser.add_argument(
        "--remote-only", action="store_true", help="update the deployed service only"
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=12.0,
        help="lifetime in hours, for the expiry warning (default: 12, the AWS maximum)",
    )
    parser.add_argument(
        "--no-secret-manager",
        action="store_true",
        help=(
            "skip the Secret Manager version. This machine and the running instance are "
            "still updated, but future Cloud Run instances keep the OLD key after a cold "
            "start — only use it when you have no gcloud access."
        ),
    )
    parser.add_argument("--status", action="store_true", help="show what is loaded, then exit")
    args = parser.parse_args()

    settings = get_settings()
    base_url = settings.public_base_url
    token = settings.trigger_token.get_secret_value()

    if args.status:
        return show_status(settings, base_url, token)

    key = read_key(args.key)

    # Validate before touching anything, so a typo doesn't half-apply.
    expires_at = datetime.now(UTC) + timedelta(hours=args.hours) if args.hours > 0 else None

    ok_local = True
    if not args.remote_only:
        try:
            stored = set_key(key, settings, expires_at=expires_at)
        except LLMKeyError as exc:
            print(f"ERROR: {exc}")
            return 1
        print(f"local: OK installed (...{stored.key[-4:]})")
        if expires_at:
            print(f"       expires {expires_at:%Y-%m-%d %H:%M} UTC")
        if write_dotenv(key):
            print("       .env updated (this is what the next deploy seeds)")
    else:
        # Still validate the format before sending it anywhere.
        if not key.startswith(KEY_PREFIXES):
            print(f"ERROR: key must start with one of {list(KEY_PREFIXES)}")
            return 1

    # A skipped remote is a FAILURE unless --local was asked for. Rotating a key that the
    # deployed service never receives is the exact problem this script exists to solve, and
    # printing "Done" after skipping it sends you away believing the service is fixed —
    # which happened: PUBLIC_BASE_URL was empty in .env, so every rotation updated only the
    # laptop while the deployed key stayed expired.
    ok_remote = True
    if not args.local:
        if not base_url or "localhost" in base_url:
            print(
                "remote: NOT UPDATED - PUBLIC_BASE_URL is unset or points at localhost.\n"
                "  The deployed service still has the OLD key.\n"
                "  Put your Cloud Run URL in .env, e.g.\n"
                "     PUBLIC_BASE_URL=https://resume-pipeline-xxxxx.a.run.app\n"
                "  (`bash scripts/deploy_cloudrun.sh` now writes it there for you), or pass\n"
                "  --local if you really only meant this machine."
            )
            ok_remote = False
        elif not token:
            print(
                "remote: NOT UPDATED - TRIGGER_TOKEN is empty, so the request cannot be\n"
                "  authenticated. The deployed service still has the OLD key."
            )
            ok_remote = False
        else:
            print(f"remote ({base_url}):")
            ok_remote = push_remote(key, base_url, token, expires_at)

    # Secret Manager is what makes the rotation outlive the current instance. Skipped for
    # --local, which by definition means "do not touch the deployment".
    ok_secret = True
    if not args.local and not args.no_secret_manager:
        print("secret manager:")
        ok_secret = push_secret_manager(key)

    if ok_local and ok_remote and ok_secret:
        where = (
            "this machine"
            if args.local
            else "this machine, the running service, and future instances"
        )
        print(f"\nDone - {where} updated.")
        print("Verify with: python scripts/set_bedrock_key.py --status")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
