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
        self.last_error: Optional[str] = None

    def _available(self) -> bool:
        if not self.enabled:
            self.last_error = "disabled in config"
            return False
        if not self.token:
            self.last_error = f"token '{self.token_env}' not set"
            return False
        if not self.repo:
            self.last_error = "repo not configured"
            return False
        self.last_error = None
        return True

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

    async def _ensure_branch(self, client: httpx.AsyncClient) -> bool:
        """Create self.branch if missing (seeds from default branch)."""
        try:
            check = await client.get(f"{GITHUB_API}/repos/{self.repo}/branches/{self.branch}")
            if check.status_code == 200:
                return True
            repo = await client.get(f"{GITHUB_API}/repos/{self.repo}")
            if repo.status_code != 200:
                logger.error(f"GitHub repo fetch failed {repo.status_code}: {repo.text[:200]}")
                return False
            default_branch = repo.json().get("default_branch", "main")
            ref_resp = await client.get(
                f"{GITHUB_API}/repos/{self.repo}/git/ref/heads/{default_branch}"
            )
            if ref_resp.status_code != 200:
                logger.error(f"GitHub branch ref fetch failed {ref_resp.status_code}")
                return False
            sha = ref_resp.json().get("object", {}).get("sha")
            create = await client.post(
                f"{GITHUB_API}/repos/{self.repo}/git/refs",
                json={"ref": f"refs/heads/{self.branch}", "sha": sha},
            )
            if create.status_code not in (200, 201):
                logger.error(f"GitHub branch create failed {create.status_code}: {create.text[:200]}")
                return False
            logger.info(f"GitHub backup branch '{self.branch}' created")
            return True
        except Exception as e:
            logger.warning(f"GitHub _ensure_branch failed: {e}")
            return False

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
            ok_branch = await self._ensure_branch(client)
            if not ok_branch:
                logger.error(f"GitHub backup aborted: branch '{self.branch}' unavailable")
                return False
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
            # 409 = concurrent write to the same path (two reports wrote the
            # same filename). Refresh the file sha and retry once.
            if r.status_code == 409:
                sha2 = await self._find_sha(client, full_path)
                if sha2:
                    body["sha"] = sha2
                    r = await client.put(url, json=body)
            if r.status_code in (200, 201):
                self.last_error = None
                logger.info(f"GitHub backup OK: {self.repo}:{self.branch} {full_path}")
                return True
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            logger.error(
                f"GitHub backup failed {r.status_code} for {full_path}: {r.text[:300]}"
            )
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"GitHub backup error for {full_path}: {e}")
        return False