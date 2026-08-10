"""Environment configuration.

Everything the pipeline needs from the outside world is declared here, so a missing
credential fails at startup with a clear name rather than deep inside a stage.

Local development reads `.env`; Render reads real environment variables. Fields with
no default are required — construct `Settings()` early (see `web/app.py` startup) so a
misconfigured deploy fails the health check instead of the first cron run.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # So `bedrock_api_key` can be set by either BEDROCK_API_KEY or the SDK's own
        # AWS_BEARER_TOKEN_BEDROCK name (declared as that field's alias).
        populate_by_name=True,
    )

    # --- LLM ---------------------------------------------------------------
    # Which surface to call Claude through.
    #   "anthropic" — first-party API, long-lived sk-ant-... key
    #   "bedrock"   — Amazon Bedrock via the Mantle (Messages API) endpoint
    llm_provider: Literal["anthropic", "bedrock"] = "bedrock"

    # First-party key. Unused when llm_provider is "bedrock".
    anthropic_api_key: SecretStr = SecretStr("")

    # Model ID *without* a provider prefix. `resolved_model()` adds Bedrock's
    # `anthropic.` prefix, so this one value works on either provider.
    llm_model: str = "claude-opus-5"

    # --- Bedrock -----------------------------------------------------------
    # Bedrock API key (starts with `bedrock-api-key-`; short-term keys use `ABSK`). These
    # expire — up to 12 hours — which is why `scripts/set_bedrock_key.py` and the
    # /admin/llm-key endpoint exist. Read from BEDROCK_API_KEY, or from the
    # AWS_BEARER_TOKEN_BEDROCK name the SDK itself recognises.
    bedrock_api_key: SecretStr = Field(default=SecretStr(""), alias="AWS_BEARER_TOKEN_BEDROCK")
    aws_region: str = "us-east-1"

    # Where the current key is cached so a restarted container doesn't lose it. The web
    # service holds it in memory too; this file is what survives a restart.
    #
    # Defaults to the platform temp directory rather than a literal "/tmp": on Windows
    # that path resolves to C:\tmp, which usually does not exist, so the cache write
    # silently failed and every shell session needed the key re-set. The container is
    # Linux, where this is /tmp as intended.
    #
    # Deliberately NOT under state/ — state/ is committed, and a credential must never be.
    # `pipeline/llm_key.py` rejects any path inside the repo rather than trusting this.
    bedrock_key_file: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "bedrock_key.json"
    )

    # --- Notion ------------------------------------------------------------
    # The integration token (ntn_...). The Resume Master page must be shared with
    # this integration, or every read returns 404 — see the master page's footer.
    notion_token: SecretStr = SecretStr("")
    notion_version: str = "2022-06-28"
    notion_master_page_id: str = "3b525b0d-3537-813e-bc87-e48ed8f823bc"

    # These are **database** IDs, not data-source (`collection://`) IDs. Notion's create
    # response returns both, and `/v1/databases/{id}/query` accepts only the former — a
    # collection ID there returns 404 `object_not_found`, which looks exactly like a
    # permissions problem and sends you hunting the integration share instead.
    # Verified: every one of these returns rows.
    notion_db_experiences: str = "16b31fc42e78450a9e4da4ae059ace67"
    notion_db_projects: str = "e47f32a6ead94d2a997be12a746b192d"
    notion_db_education: str = "2a9b9e512a844f2f86605ec86a0eebaa"
    notion_db_skills: str = "53723269fbbf41e09db6a97accc6c9ff"
    notion_db_publications: str = "8481129268544760b97339b7605f3fb7"
    notion_db_profile: str = "05427da04c764d5b885abea89967a29a"
    notion_db_runs: str = "6009f1883b3345caa2f05a051a0b5c67"

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

    # --- LLM helpers -------------------------------------------------------
    def resolved_model(self) -> str:
        """The model ID as the configured provider expects it.

        Bedrock model IDs carry an `anthropic.` prefix; the first-party API rejects it.
        Keeping `llm_model` prefix-free means switching providers is one env var, not a
        second string to remember to change.
        """
        if self.llm_provider == "bedrock":
            return (
                self.llm_model
                if self.llm_model.startswith("anthropic.")
                else f"anthropic.{self.llm_model}"
            )
        return self.llm_model.removeprefix("anthropic.")

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
