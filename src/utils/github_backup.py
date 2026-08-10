"""
GitHub repo backup - archives scan/signal snapshots to a GitHub repo
via the Contents API using a Personal Access Token (PAT).

Why: Render (free tier) uses an ephemeral disk - bot_state.json on disk can
be lost on restart. Archiving each scan to a private GitHub repo keeps data.

Writes go to the `archive` branch (not `main`), so Render's auto-deploy on
main is not triggered by backups.

Usage in bot:
    backup = GithubBackup(config)
    await backup.upload_json("scans/2026-08-11/1500.json", payload)
"""
import os
import base64
import logging
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger("github_backup")

GITHUB_API = "https://api.github.com"


class GithubBackup:
    """Upload JSON payloads to a GitHub repo branch via Contents API."""

    def __init__(self, config: Dict[str, Any]):
        cfg = config.get("backup", {}).get("github", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.repo = cfg.get("repo", "donozoyout-ux/Nasdq13")
        self.branch = cfg.get("branch", "archive")
        self.root = cfg.get("path", "data/backups").strip("/")
        self.token_env = cfg.get("token_env", "GITHUB_BACKUP_TOKEN")
        self.token = os.getenv(self.token_env, "")
        self._client: Optional[httpx.AsyncClient] = None

    def _available(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.repo)

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _find_sha(self, client: httpx.AsyncClient, path: str) -> Optional[str]:
        """Return the current file sha (if it exists) so we can overwrite."""
        try:
            url = f"{GITHUB_API}/repos/{self.repo}/contents/{path}"
            r = await client.get(url, params={"ref": self.branch})
            if r.status_code == 200:
                return r.json().get("sha")
        except Exception as e:
            logger.warning(f"GitHub _find_sha failed for {path}: {e}")
        return None

    async def upload_json(self, path: str, payload: Dict[str, Any]) -> bool:
        """Create or overwrite a JSON file at repo:root/{path} on self.branch.
        Returns True on success, False on missing token / error."""
        if not self._available():
            logger.warning(
                "GitHub backup disabled (enabled=%s, token set=%s, repo=%s)",
                self.enabled, bool(self.token), self.repo,
            )
            return False

        full_path = f"{self.root}/{path}" if self.root else path
        content = base64.b64encode(
            __import__("json").dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        ).decode("utf-8")

        try:
            client = await self._client_get()
            sha = await self._find_sha(client, full_path)
            body: Dict[str, Any] = {
                "message": f"backup: {path}",
                "branch": self.branch,
                "content": content,
            }
            if sha:
                body["sha"] = sha
            url = f"{GITHUB_API}/repos/{self.repo}/contents/{full_path}"
            r = await client.put(url, json=body)
            if r.status_code in (200, 201):
                logger.info(f"GitHub backup OK: {self.repo}:{self.branch} {full_path}")
                return True
            logger.error(
                f"GitHub backup failed {r.status_code} for {full_path}: {r.text[:300]}"
            )
        except Exception as e:
            logger.error(f"GitHub backup error for {full_path}: {e}")
        return False