"""Verify the Notion token and connection before deploying anything.

    python scripts/check_notion.py

Reads NOTION_TOKEN from .env or the environment. Checks the three things that go wrong,
in the order they go wrong:

1. Is the token valid at all?
2. Is the integration connected to the Resume Master page? (A valid token with no
   connection returns 404 on every read — the single most common setup mistake.)
3. Do the seven databases resolve, and how many Approved rows does each have?

Read-only: never writes to Notion.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from pipeline.config import get_settings  # noqa: E402

OK = "OK"
BAD = "ERROR:"


async def main() -> int:
    settings = get_settings()
    token = settings.notion_token.get_secret_value()

    if not token:
        print(f"{BAD} NOTION_TOKEN is empty.")
        print("   Put it in .env as NOTION_TOKEN=ntn_... (see .env.example)")
        return 1

    prefix = token.split("_")[0] if "_" in token else token[:6]
    print(f"  token starts with {prefix!r}, length {len(token)}")
    if not token.startswith(("ntn_", "secret_")):
        print(
            f"{BAD} That doesn't look like an internal integration token.\n"
            "   Expected ntn_... (current) or secret_... (legacy).\n"
            "   An OAuth access token won't work here — it expires and needs refreshing.\n"
            "   Create an *Internal* Integration at https://www.notion.so/my-integrations"
        )
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": settings.notion_version,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(
        base_url="https://api.notion.com/v1", headers=headers, timeout=30.0
    ) as client:
        # --- 1. token validity -------------------------------------------------
        response = await client.get("/users/me")
        if response.status_code == 401:
            print(f"{BAD} 401 - the token is invalid or was revoked.")
            return 1
        if response.status_code != 200:
            print(f"{BAD} /users/me returned {response.status_code}: {response.text[:200]}")
            return 1

        bot = response.json()
        print(f"{OK} token valid - integration {bot.get('name', '(unnamed)')!r}")

        # --- 2. page connection ------------------------------------------------
        page_id = settings.notion_master_page_id
        response = await client.get(f"/pages/{page_id}")
        if response.status_code == 404:
            print(
                f"{BAD} 404 reading the Resume Master page.\n"
                "   The token is valid but the integration is NOT connected to the page.\n"
                f"   Open https://app.notion.com/p/{page_id.replace('-', '')}\n"
                "   then: top-right ... -> Connections -> add your integration."
            )
            return 1
        if response.status_code != 200:
            print(f"{BAD} /pages/{page_id} returned {response.status_code}")
            return 1
        print(f"{OK} Resume Master page reachable")

        # --- 3. databases ------------------------------------------------------
        databases = {
            "Experiences": settings.notion_db_experiences,
            "Projects": settings.notion_db_projects,
            "Education": settings.notion_db_education,
            "Skills": settings.notion_db_skills,
            "Publications": settings.notion_db_publications,
            "Profile": settings.notion_db_profile,
            "Resume Runs": settings.notion_db_runs,
        }

        print()
        failures = 0
        total_approved = 0
        for name, database_id in databases.items():
            response = await client.post(f"/databases/{database_id}/query", json={"page_size": 100})
            if response.status_code != 200:
                print(f"  {BAD} {name:14} {response.status_code} - {response.text[:80]}")
                failures += 1
                # A 404 here has two very different causes and the API reports both as
                # object_not_found. Distinguish them: if a plain GET on the same id also
                # 404s but the id is a valid database when fetched, it is a permissions
                # problem; if the id simply isn't a database, it is the wrong kind of id.
                if response.status_code == 404:
                    probe = await client.get(f"/databases/{database_id}")
                    if probe.status_code == 200:
                        print("       ^ the id exists but query was denied - check the share")
                    else:
                        print(
                            "       ^ that id is not a database. Notion returns two ids per "
                            "database:\n"
                            "         the database id, and a data-source (collection://) id. "
                            "Only the\n"
                            "         former works with /databases/{id}/query."
                        )
                continue

            rows = response.json().get("results", [])
            included = sum(
                1
                for row in rows
                if row["properties"].get("Include in Resume", {}).get("checkbox")
                and (
                    (select := row["properties"].get("Status", {}).get("select")) is None
                    or select.get("name") == "Approved"
                )
            )
            total_approved += included
            note = f"{len(rows):>3} row(s), {included} ready for the resume"
            print(f"  {OK} {name:14} {note}")

        print()
        if failures:
            print(f"{BAD} {failures} database(s) unreachable - check the NOTION_DB_* ids.")
            return 1

        if total_approved == 0:
            print(
                f"{BAD} No rows are ready. A run would fail with "
                '"resume has no education, experience, or projects".\n'
                "   In Notion, set Status = Approved AND tick Include in Resume."
            )
            return 1

        print(f"{OK} Notion is ready - {total_approved} row(s) will be included.")
        print("   Next: try  python scripts/local_run.py   to render from real data.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
