"""GitLab REST 客户端（stdlib-only）。

与 GitHubClient 同构：暴露 ``enabled`` / ``branch_exists`` / ``list_commits``
/ ``list_pulls`` / ``get_branch_activity`` / ``has_merged_to_targets``，
便于 Fetcher / SyncService 通过统一接口消费两个 provider。

GitLab 概念映射到本项目数据契约：
- Merge Request → ``PullRequest``（语义一致）
- ``target_branch`` → ``base_branch``
- ``source_branch`` → ``head_branch``
- ``state == "merged"`` 视为 ``merged=True``
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


class GitLabError(RuntimeError):
    pass


class GitLabClient:
    """GitLab REST 客户端。

    ``project`` 是 ``group/subgroup/.../name`` 形式的项目路径（URL 编码自动处理）。
    """

    def __init__(
        self,
        token: str = "",
        api_base: str = "https://git.ziniao.com/api/v4",
        timeout: float = 20.0,
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # ----------------------------------------------------------
    # HTTP 底层
    # ----------------------------------------------------------

    @staticmethod
    def _project_id(project: str) -> str:
        return urllib.parse.quote(project, safe="")

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.api_base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        req = urllib.request.Request(
            url,
            headers={
                "PRIVATE-TOKEN": self.token,
                "Accept": "application/json",
                "User-Agent": "progress-report-bot/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            body = ""
            try:
                body = he.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise GitLabError(f"GitLab {he.code} {he.reason}: {body[:300]}") from he
        except urllib.error.URLError as ue:
            raise GitLabError(f"GitLab 网络错误: {ue}") from ue

    # ----------------------------------------------------------
    # 资源
    # ----------------------------------------------------------

    def branch_exists(self, project: str, branch: str) -> bool:
        try:
            self._get(
                f"/projects/{self._project_id(project)}/repository/branches/"
                f"{urllib.parse.quote(branch, safe='')}"
            )
            return True
        except GitLabError as ge:
            if " 404 " in f" {ge} ":
                return False
            raise

    def list_commits(
        self,
        project: str,
        ref: Optional[str] = None,
        since: Optional[datetime] = None,
        per_page: int = 100,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"per_page": per_page}
        if ref:
            params["ref_name"] = ref
        if since:
            params["since"] = since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        return (
            self._get(
                f"/projects/{self._project_id(project)}/repository/commits", params=params
            )
            or []
        )

    def list_merge_requests(
        self,
        project: str,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        state: str = "all",  # opened | closed | merged | all
        per_page: int = 50,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"state": state, "per_page": per_page}
        if source_branch:
            params["source_branch"] = source_branch
        if target_branch:
            params["target_branch"] = target_branch
        return (
            self._get(
                f"/projects/{self._project_id(project)}/merge_requests", params=params
            )
            or []
        )

    # ----------------------------------------------------------
    # 业务封装（与 GitHubClient 同形）
    # ----------------------------------------------------------

    def get_branch_activity(
        self,
        repo: str,
        branch: str,
        since: Optional[datetime] = None,
        target_branches: Optional[List[str]] = None,
    ) -> BranchActivity:
        if not self.enabled:
            return BranchActivity(repo=repo, branch=branch, exists=False)

        exists = self.branch_exists(repo, branch)
        activity = BranchActivity(repo=repo, branch=branch, exists=exists)
        if not exists:
            return activity

        try:
            raw_commits = self.list_commits(repo, ref=branch, since=since)
        except GitLabError as e:
            logger.warning("list_commits 失败 %s@%s: %s", repo, branch, e)
            raw_commits = []
        for c in raw_commits:
            date_str = c.get("authored_date") or c.get("committed_date") or ""
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now()
            activity.commits.append(
                GitCommit(
                    sha=str(c.get("short_id") or c.get("id") or "")[:8],
                    message=(c.get("title") or c.get("message") or "")[:120],
                    author=str(c.get("author_name") or ""),
                    date=dt,
                    url=str(c.get("web_url") or ""),
                )
            )

        try:
            raw_mrs = self.list_merge_requests(repo, source_branch=branch, state="all")
        except GitLabError as e:
            logger.warning("list_merge_requests 失败 %s@%s: %s", repo, branch, e)
            raw_mrs = []
        for m in raw_mrs:
            state = str(m.get("state") or "")
            activity.pull_requests.append(
                PullRequest(
                    number=int(m.get("iid") or 0),
                    title=str(m.get("title") or ""),
                    state="merged" if state == "merged" else state,
                    author=((m.get("author") or {}).get("username") or ""),
                    head_branch=str(m.get("source_branch") or branch),
                    base_branch=str(m.get("target_branch") or ""),
                    url=str(m.get("web_url") or ""),
                    merged=(state == "merged"),
                )
            )
        return activity

    def has_merged_to_targets(
        self,
        repo: str,
        branch: str,
        target_branches: List[str],
    ) -> Optional[PullRequest]:
        if not self.enabled or not target_branches:
            return None
        targets_lower = {b.lower() for b in target_branches}
        # GitLab API 支持 target_branch 过滤，但需要一一查
        for tgt in target_branches:
            try:
                mrs = self.list_merge_requests(
                    repo,
                    source_branch=branch,
                    target_branch=tgt,
                    state="merged",
                )
            except GitLabError as e:
                logger.warning("查 MR 失败 %s %s→%s: %s", repo, branch, tgt, e)
                continue
            for m in mrs:
                base = str(m.get("target_branch") or "").lower()
                if base in targets_lower:
                    return PullRequest(
                        number=int(m.get("iid") or 0),
                        title=str(m.get("title") or ""),
                        state="merged",
                        author=((m.get("author") or {}).get("username") or ""),
                        head_branch=branch,
                        base_branch=str(m.get("target_branch") or ""),
                        url=str(m.get("web_url") or ""),
                        merged=True,
                    )
        return None
