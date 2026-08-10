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
"""

from __future__ import annotations

import argparse
import getpass
import json
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
    else:
        # Still validate the format before sending it anywhere.
        if not key.startswith(KEY_PREFIXES):
            print(f"ERROR: key must start with one of {list(KEY_PREFIXES)}")
            return 1

    ok_remote = True
    if not args.local:
        if not base_url or "localhost" in base_url:
            print("remote: skipped (PUBLIC_BASE_URL is unset or local)")
        elif not token:
            print("remote: skipped (TRIGGER_TOKEN is not set - cannot authenticate)")
        else:
            print(f"remote ({base_url}):")
            ok_remote = push_remote(key, base_url, token, expires_at)

    if ok_local and ok_remote:
        print("\nDone. Verify with: python scripts/set_bedrock_key.py --status")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
