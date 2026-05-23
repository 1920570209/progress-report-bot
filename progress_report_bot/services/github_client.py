"""GitHub REST 客户端（最小实现，stdlib-only）。

只覆盖本项目实际需要的接口：
- 列分支某时间窗口的 commits
- 列 head=branch 的 PR 状态

P0 不强制依赖 GITHUB_TOKEN：未提供时 fetcher 会跳过 GitHub 段落。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import BranchActivity, GitCommit, PullRequest

logger = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        token: str = "",
        api_base: str = "https://api.github.com",
        timeout: float = 20.0,
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.api_base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}" if self.token else "",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "progress-report-bot/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="replace")
            raise GitHubError(f"GitHub {he.code} {he.reason}: {body[:300]}") from he
        except urllib.error.URLError as ue:
            raise GitHubError(f"GitHub 网络错误: {ue}") from ue

    def list_commits(
        self,
        repo: str,
        sha: Optional[str] = None,
        since: Optional[datetime] = None,
        per_page: int = 100,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"per_page": per_page}
        if sha:
            params["sha"] = sha
        if since:
            params["since"] = since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        return self._get(f"/repos/{repo}/commits", params=params) or []

    def list_pulls(
        self,
        repo: str,
        head: Optional[str] = None,
        state: str = "all",
        per_page: int = 50,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"state": state, "per_page": per_page}
        if head:
            params["head"] = head
        return self._get(f"/repos/{repo}/pulls", params=params) or []

    def branch_exists(self, repo: str, branch: str) -> bool:
        try:
            self._get(f"/repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}")
            return True
        except GitHubError as ge:
            if " 404 " in f" {ge} ":
                return False
            raise

    # ----------------------------------------------------------
    # 业务封装
    # ----------------------------------------------------------

    def get_branch_activity(
        self,
        repo: str,
        branch: str,
        since: Optional[datetime] = None,
        target_branches: Optional[List[str]] = None,
    ) -> BranchActivity:
        """拉取分支 commits + 关联 PR，并标记是否已合并到目标 base 分支。"""
        if not self.enabled:
            return BranchActivity(repo=repo, branch=branch, exists=False)

        exists = self.branch_exists(repo, branch)
        activity = BranchActivity(repo=repo, branch=branch, exists=exists)
        if not exists:
            return activity

        raw_commits = self.list_commits(repo, sha=branch, since=since)
        for c in raw_commits:
            commit = c.get("commit") or {}
            author = (commit.get("author") or {}).get("name") or ""
            date_str = (commit.get("author") or {}).get("date") or ""
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now()
            activity.commits.append(
                GitCommit(
                    sha=str(c.get("sha") or "")[:7],
                    message=(commit.get("message") or "").split("\n")[0][:120],
                    author=author,
                    date=dt,
                    url=str(c.get("html_url") or ""),
                )
            )

        # PR: head=owner:branch（GitHub API 支持 owner:branch 或仅 branch）
        owner = repo.split("/")[0] if "/" in repo else ""
        head = f"{owner}:{branch}" if owner else branch
        raw_prs = self.list_pulls(repo, head=head, state="all")
        targets = {b.lower() for b in (target_branches or [])}
        for p in raw_prs:
            base = ((p.get("base") or {}).get("ref") or "").lower()
            merged = bool(p.get("merged_at"))
            pr = PullRequest(
                number=int(p.get("number") or 0),
                title=str(p.get("title") or ""),
                state=str(p.get("state") or ""),
                author=((p.get("user") or {}).get("login") or ""),
                head_branch=((p.get("head") or {}).get("ref") or branch),
                base_branch=((p.get("base") or {}).get("ref") or ""),
                url=str(p.get("html_url") or ""),
                merged=merged,
            )
            activity.pull_requests.append(pr)

        return activity

    def has_merged_to_targets(
        self,
        repo: str,
        branch: str,
        target_branches: List[str],
    ) -> Optional[PullRequest]:
        """若存在 head=branch 且已 merged 到 target_branches 白名单内 base 的 PR，返回该 PR。"""
        if not self.enabled or not target_branches:
            return None
        owner = repo.split("/")[0] if "/" in repo else ""
        head = f"{owner}:{branch}" if owner else branch
        targets = {b.lower() for b in target_branches}
        try:
            raw_prs = self.list_pulls(repo, head=head, state="closed")
        except GitHubError as e:
            logger.warning("查 PR 失败 repo=%s branch=%s: %s", repo, branch, e)
            return None
        for p in raw_prs:
            if not p.get("merged_at"):
                continue
            base = ((p.get("base") or {}).get("ref") or "").lower()
            if base in targets:
                return PullRequest(
                    number=int(p.get("number") or 0),
                    title=str(p.get("title") or ""),
                    state="merged",
                    author=((p.get("user") or {}).get("login") or ""),
                    head_branch=branch,
                    base_branch=((p.get("base") or {}).get("ref") or ""),
                    url=str(p.get("html_url") or ""),
                    merged=True,
                )
        return None
