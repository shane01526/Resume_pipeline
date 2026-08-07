"""Blob storage behind `RunStore`, so state survives a container that doesn't.

Two backends, one interface:

- `LocalStorage` writes to the filesystem. Used for local development and tests.
- `GitHubStorage` reads and writes through the GitHub Contents API. Used on Cloud Run,
  where the container disk is ephemeral and consecutive requests can land on different
  instances — so a run created by one request must be readable by the next.

Why the Contents API rather than a git clone: Cloud Run gives no persistent disk and can
start a fresh instance per request. Cloning on every cold start would work but costs
seconds and megabytes per request; the API reads exactly the one file a request needs.
The repository is still the durable store — this just skips the working copy.

The abstraction is a flat key-value map of `path -> bytes`, because that is all `RunStore`
ever needed from the filesystem.
"""

from __future__ import annotations

import base64
import logging
import shutil
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from pipeline.config import Settings

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class StorageError(RuntimeError):
    pass


class Storage(ABC):
    """Flat `path -> bytes` store. Paths are repo-relative POSIX strings."""

    @abstractmethod
    def read(self, path: str) -> bytes | None:
        """File contents, or None if absent."""

    @abstractmethod
    def write(self, path: str, data: bytes, message: str) -> None:
        """Create or replace a file. `message` is the commit message where relevant."""

    @abstractmethod
    def delete_prefix(self, prefix: str, message: str) -> int:
        """Delete every file under `prefix`. Returns how many were removed."""

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Paths directly under `prefix`, files and directories alike."""

    def read_text(self, path: str) -> str | None:
        data = self.read(path)
        return data.decode("utf-8") if data is not None else None

    def write_text(self, path: str, text: str, message: str) -> None:
        self.write(path, text.encode("utf-8"), message)


class LocalStorage(Storage):
    """Filesystem-backed. The default for local runs and every test."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _full(self, path: str) -> Path:
        # Containment check: paths reach here from URLs in some code paths.
        resolved = (self._root / path).resolve()
        root = self._root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise StorageError(f"path escapes the storage root: {path}") from exc
        return resolved

    def read(self, path: str) -> bytes | None:
        full = self._full(path)
        return full.read_bytes() if full.is_file() else None

    def write(self, path: str, data: bytes, message: str) -> None:  # noqa: ARG002
        full = self._full(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        # Temp file plus replace, so a crash can't leave a half-written run record.
        tmp = full.with_suffix(full.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(full)

    def delete_prefix(self, prefix: str, message: str) -> int:  # noqa: ARG002
        full = self._full(prefix)
        if not full.exists():
            return 0
        count = sum(1 for p in full.rglob("*") if p.is_file())
        if full.is_dir():
            shutil.rmtree(full)
        else:
            full.unlink()
            count = 1
        return count

    def list_prefix(self, prefix: str) -> list[str]:
        full = self._full(prefix)
        if not full.is_dir():
            return []
        return sorted(f"{prefix}/{child.name}".lstrip("/") for child in full.iterdir())


class GitHubStorage(Storage):
    """GitHub Contents API-backed, for hosts with no persistent disk.

    Every write is a commit, which is the same durability model as the local backend
    (`state/` is committed there too) — just without a working copy.

    A small in-process cache keeps a single request from re-fetching the same file, since
    the diff page reads a run record several times while rendering.
    """

    def __init__(self, settings: Settings) -> None:
        token = settings.github_token.get_secret_value()
        if not token:
            raise StorageError("GITHUB_TOKEN is required for GitHub-backed storage")
        self._settings = settings
        self._repo = settings.github_repo
        self._branch = settings.git_branch
        self._client = httpx.Client(
            base_url=GITHUB_API,
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        # path -> (sha, bytes). The sha is required to update or delete a file.
        self._cache: dict[str, tuple[str, bytes]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    # --- reads --------------------------------------------------------------
    def read(self, path: str) -> bytes | None:
        with self._lock:
            if cached := self._cache.get(path):
                return cached[1]

        response = self._client.get(
            f"/repos/{self._repo}/contents/{path}", params={"ref": self._branch}
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise StorageError(f"GET {path} failed ({response.status_code}): {response.text[:200]}")

        payload = response.json()
        if isinstance(payload, list):
            raise StorageError(f"{path} is a directory, not a file")

        # Files over 1MB come back without content and need the blob API. Artifacts are
        # PDFs, so this is a normal path rather than an edge case.
        if payload.get("content"):
            data = base64.b64decode(payload["content"])
        else:
            blob = self._client.get(f"/repos/{self._repo}/git/blobs/{payload['sha']}")
            if blob.status_code >= 400:
                raise StorageError(f"blob fetch for {path} failed ({blob.status_code})")
            data = base64.b64decode(blob.json()["content"])

        with self._lock:
            self._cache[path] = (payload["sha"], data)
        return data

    def list_prefix(self, prefix: str) -> list[str]:
        response = self._client.get(
            f"/repos/{self._repo}/contents/{prefix}", params={"ref": self._branch}
        )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise StorageError(f"LIST {prefix} failed ({response.status_code})")
        payload = response.json()
        if not isinstance(payload, list):
            return [prefix]
        return sorted(entry["path"] for entry in payload)

    # --- writes -------------------------------------------------------------
    def write(self, path: str, data: bytes, message: str) -> None:
        body: dict[str, object] = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self._branch,
        }
        # An update needs the current sha; a create must not send one.
        if sha := self._sha_of(path):
            body["sha"] = sha

        response = self._client.put(f"/repos/{self._repo}/contents/{path}", json=body)

        if response.status_code == 409:
            # Another writer moved the branch. Re-read the sha and retry once — with one
            # user and a weekly schedule this is rare, and a second conflict means
            # something is genuinely wrong.
            self._forget(path)
            if sha := self._sha_of(path):
                body["sha"] = sha
            response = self._client.put(f"/repos/{self._repo}/contents/{path}", json=body)

        if response.status_code >= 400:
            raise StorageError(f"PUT {path} failed ({response.status_code}): {response.text[:300]}")

        with self._lock:
            self._cache[path] = (response.json()["content"]["sha"], data)

    def delete_prefix(self, prefix: str, message: str) -> int:
        deleted = 0
        for path in self._walk(prefix):
            sha = self._sha_of(path)
            if sha is None:
                continue
            response = self._client.request(
                "DELETE",
                f"/repos/{self._repo}/contents/{path}",
                json={"message": message, "sha": sha, "branch": self._branch},
            )
            if response.status_code >= 400:
                # Best-effort: a failed cleanup must not fail the operation that triggered
                # it (rejecting a run, for instance).
                log.warning("could not delete %s (%s)", path, response.status_code)
                continue
            self._forget(path)
            deleted += 1
        return deleted

    # --- helpers ------------------------------------------------------------
    def _sha_of(self, path: str) -> str | None:
        with self._lock:
            if cached := self._cache.get(path):
                return cached[0]
        response = self._client.get(
            f"/repos/{self._repo}/contents/{path}", params={"ref": self._branch}
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return None
        payload = response.json()
        return payload.get("sha") if isinstance(payload, dict) else None

    def _walk(self, prefix: str) -> list[str]:
        """Every file under `prefix`, recursively. Directories have no sha to delete."""
        files: list[str] = []
        response = self._client.get(
            f"/repos/{self._repo}/contents/{prefix}", params={"ref": self._branch}
        )
        if response.status_code != 200:
            return files
        payload = response.json()
        if not isinstance(payload, list):
            return [prefix]
        for entry in payload:
            if entry["type"] == "dir":
                files.extend(self._walk(entry["path"]))
            else:
                files.append(entry["path"])
        return files

    def _forget(self, path: str) -> None:
        with self._lock:
            self._cache.pop(path, None)


def build_storage(settings: Settings) -> Storage:
    """The backend the environment calls for.

    GitHub-backed when `STORAGE_BACKEND=github` — which Cloud Run needs, because its disk
    is ephemeral and requests can land on different instances. Filesystem otherwise, which
    keeps local development and every test off the network.
    """
    if settings.storage_backend == "github":
        log.info("using GitHub-backed storage (%s@%s)", settings.github_repo, settings.git_branch)
        return GitHubStorage(settings)
    return LocalStorage(settings.repo_root)
