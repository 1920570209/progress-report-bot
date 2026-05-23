"""根据 Config 选择 GitHub / GitLab 客户端的工厂。

调用方按统一接口消费：``enabled`` / ``get_branch_activity`` / ``has_merged_to_targets``。
"""

from __future__ import annotations

import logging
from typing import Union

from ..config import Config
from .github_client import GitHubClient
from .gitlab_client import GitLabClient
from .local_git_client import LocalGitClient

logger = logging.getLogger(__name__)

GitClient = Union[LocalGitClient, GitHubClient, GitLabClient, "NullGitClient"]


class NullGitClient:
    """Git-disabled 模式（``GIT_PROVIDER=none`` 或自动降级）。

    所有方法返回"未启用"语义。Skill 真正可分发：其他项目组若无开发分支字段，
    本工具仍能跑通飞书纯流转分析。
    """

    enabled = False

    def branch_exists(self, repo, branch):  # noqa: D401, ARG002
        return False

    def get_branch_activity(self, repo, branch, since=None, target_branches=None):  # noqa: ARG002
        from ..models import BranchActivity
        return BranchActivity(repo=str(repo), branch=branch or "", exists=False)

    def has_merged_to_targets(self, repo, branch, target_branches):  # noqa: ARG002
        return None


def make_git_client(cfg: Config) -> GitClient:
    provider = (cfg.git_provider or "").strip().lower()
    if provider in {"none", "off", "disabled", ""}:
        logger.info("Git provider: none（纯飞书模式，跳过 git enrich）")
        return NullGitClient()
    if provider == "local":
        client = LocalGitClient(
            repo_path=cfg.local_git_repo_path,
            remote_prefix=cfg.local_git_remote_prefix,
            repo_root=cfg.local_git_repo_root,
        )
        if not client.enabled:
            logger.warning(
                "Git provider=local 但既找不到有效仓库（LOCAL_GIT_REPO_PATH=%r），"
                "也找不到容器（LOCAL_GIT_REPO_ROOT=%r），将自动降级为纯飞书模式。",
                cfg.local_git_repo_path,
                cfg.local_git_repo_root,
            )
        return client
    if provider == "gitlab":
        client = GitLabClient(cfg.gitlab_token, cfg.gitlab_api_base)
        if client.enabled:
            logger.info(
                "Git provider: GitLab (api_base=%s, default_project=%s)",
                cfg.gitlab_api_base,
                cfg.gitlab_default_project or "(none)",
            )
        return client
    client = GitHubClient(cfg.github_token, cfg.github_api_base)
    if client.enabled:
        logger.info("Git provider: GitHub (api_base=%s)", cfg.github_api_base)
    return client


def resolve_repo(cfg: Config, work_item) -> str:
    """返回单个 repo 标识（向后兼容）。local 模式建议改用 resolve_repos。"""
    paths = resolve_repos(cfg, work_item)
    return paths[0] if paths else ""


def resolve_repos(cfg: Config, work_item) -> list:
    """根据工作项「选择仓库」字段返回**所有**应扫描的 repo 路径/标识列表。

    - local 模式：把 short code 映射到 LOCAL_GIT_REPO_ROOT 下的子目录路径
    - github/gitlab 模式：返回 owner/repo 或 group/proj 列表
    """
    provider = (cfg.git_provider or "").strip().lower()
    if provider == "local":
        if work_item.repos:
            mapped = cfg.resolve_repo_paths(list(work_item.repos))
            if mapped:
                return [p for p, _ in mapped]
        if cfg.local_git_repo_path:
            return [cfg.local_git_repo_path]
        return []

    # 远程 provider：直接用 repos 字符串
    if work_item.repos:
        items = [r for r in work_item.repos if isinstance(r, str) and "/" in r]
        if items:
            return items
    if provider == "gitlab":
        return [cfg.gitlab_default_project] if cfg.gitlab_default_project else []
    return [cfg.github_default_repo] if cfg.github_default_repo else []
