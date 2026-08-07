"""Environment configuration.

Everything the pipeline needs from the outside world is declared here, so a missing
credential fails at startup with a clear name rather than deep inside a stage.

Local development reads `.env`; Render reads real environment variables. Fields with
no default are required — construct `Settings()` early (see `web/app.py` startup) so a
misconfigured deploy fails the health check instead of the first cron run.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- LLM ---------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-opus-5"

    # --- Notion ------------------------------------------------------------
    # The integration token (ntn_...). The Resume Master page must be shared with
    # this integration, or every read returns 404 — see the master page's footer.
    notion_token: SecretStr = SecretStr("")
    notion_version: str = "2022-06-28"
    notion_master_page_id: str = "3b525b0d-3537-813e-bc87-e48ed8f823bc"
    notion_db_experiences: str = "bd8fd592-9e98-4fe6-9d7e-0fb71aeeb8f8"
    notion_db_projects: str = "9b54da5d-c48e-4d9a-a6aa-87083090a6ae"
    notion_db_education: str = "abe08012-2e03-40c6-b4f6-2357830d73bc"
    notion_db_skills: str = "98449619-495f-4fa8-98ce-20fc066bfffb"
    notion_db_publications: str = "721b08ab-0db4-42d7-a500-5e9f1f42e9d5"
    notion_db_profile: str = "03bae5bc-09f9-4267-a2a8-d9316e5f8512"
    notion_db_runs: str = "a5f3a01d-a436-42e1-88bc-017bcdd7232c"

    # --- Slack -------------------------------------------------------------
    slack_bot_token: SecretStr = SecretStr("")
    slack_signing_secret: SecretStr = SecretStr("")
    slack_dm_channel: str = Field(default="", description="Channel or user ID to notify")

    # --- GitHub (state + published artifacts live in the repo) -------------
    github_token: SecretStr = SecretStr("")
    github_repo: str = "shane01526/Resume_pipeline"
    git_branch: str = "main"
    git_author_name: str = "resume-pipeline"
    git_author_email: str = "bot@resume-pipeline.local"

    # --- Web / approval ----------------------------------------------------
    public_base_url: str = Field(
        default="http://localhost:8000", description="Used to build approval links"
    )
    approval_hmac_secret: SecretStr = SecretStr("dev-only-insecure-secret")
    trigger_token: SecretStr = SecretStr("dev-only-insecure-token")
    approval_timeout_hours: float = 72.0

    # --- Rendering ---------------------------------------------------------
    # Which renderers to run. Dropping one here also drops it from the diff page.
    renderers: list[str] = Field(default_factory=lambda: ["html", "latex", "docx"])
    pdftoppm_dpi: int = 110
    tectonic_bin: str = "tectonic"

    # --- Storage -----------------------------------------------------------
    # "local" writes run state to the filesystem; "github" writes through the Contents API.
    # Cloud Run needs "github": its disk is ephemeral and consecutive requests can land on
    # different instances, so a run created by one request must be readable by the next.
    # Render and local development can use "local", where state/ is a committed directory.
    storage_backend: Literal["local", "github"] = "local"

    # --- Paths -------------------------------------------------------------
    # A real field, not a hardcoded constant, so a test can point the whole pipeline at a
    # temp directory: `Settings(repo_root=tmp_path)`. Every path below derives from it.
    # This was learned by writing a test that monkeypatched the property, missed because
    # get_settings() is cached, and wrote into the production sources/ directory.
    repo_root: Path = REPO_ROOT

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("renderers", mode="before")
    @classmethod
    def _split_renderers(cls, v: object) -> object:
        # Render's dashboard can only supply strings, so accept "html,latex".
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    # --- Derived paths -----------------------------------------------------
    @property
    def sources_dir(self) -> Path:
        return self.repo_root / "sources"

    @property
    def output_dir(self) -> Path:
        return self.repo_root / "output"

    @property
    def state_dir(self) -> Path:
        return self.repo_root / "state"

    @property
    def runs_dir(self) -> Path:
        return self.repo_root / "state" / "runs"

    @property
    def approved_snapshot(self) -> Path:
        """Last approved resume.json — the diff baseline."""
        return self.repo_root / "state" / "approved.json"

    @property
    def sources_index(self) -> Path:
        """SHA256 → processed record, so ingest only handles new or changed files."""
        return self.repo_root / "state" / "sources.json"

    def git_remote_url(self) -> str:
        """HTTPS remote with the token embedded, for push from a container."""
        token = self.github_token.get_secret_value()
        if not token:
            return f"https://github.com/{self.github_repo}.git"
        return f"https://x-access-token:{token}@github.com/{self.github_repo}.git"

    def missing_for_publish(self) -> list[str]:
        """Credentials required to actually publish. Checked before a run starts."""
        missing = []
        if not self.notion_token.get_secret_value():
            missing.append("NOTION_TOKEN")
        if not self.github_token.get_secret_value():
            missing.append("GITHUB_TOKEN")
        if not self.slack_bot_token.get_secret_value():
            missing.append("SLACK_BOT_TOKEN")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
