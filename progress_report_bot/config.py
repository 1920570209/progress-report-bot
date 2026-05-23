"""配置加载：优先级 = CLI 参数 > 环境变量 > .env 文件 > 默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _load_dotenv_if_present(env_file: Optional[Path] = None) -> None:
    """尽量轻量地加载 .env，不强依赖 python-dotenv。"""
    if env_file is None:
        env_file = Path.cwd() / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_file, override=False)
        return
    except ImportError:
        pass

    # Fallback: 极简解析器
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


@dataclass
class Config:
    # --- Meego MCP ---
    meego_mcp_url: str = "https://project.feishu.cn/mcp_server/v1"
    meego_mcp_token: str = ""
    meego_project_key: str = ""
    meego_report_carrier_id: str = ""
    meego_report_carrier_type_key: str = ""
    # 可选：演示/联调时只关注单个工作项（留空=全项目）
    meego_focus_work_item_id: str = ""

    # --- Git provider 切换：local（推荐 skill 默认） | gitlab | github ---
    git_provider: str = "local"

    # --- Local git ---
    local_git_repo_path: str = "."          # 单仓库模式：仓库根目录
    local_git_repo_root: str = ""           # 多仓库容器目录（每个子目录是一个 git repo）
    local_git_remote_prefix: str = "origin/"  # 查分支时的远端前缀，空=只看本地
    # 飞书「选择仓库」short code → 仓库子目录名（或绝对路径）映射
    # 形如 "&3neskoa5d=A8-cloudsum-server-script,&ey8ghlypk=A8-cloudsum-server-cloudpool"
    repo_id_map: str = ""

    # --- GitHub ---
    github_api_base: str = "https://api.github.com"
    github_token: str = ""
    github_default_repo: str = ""

    # --- GitLab ---
    gitlab_api_base: str = "https://git.ziniao.com/api/v4"
    gitlab_token: str = ""
    gitlab_default_project: str = ""  # 形如 group/subgroup/name

    merge_target_branches: str = "test"

    # 安全护栏：演示期间只允许 sync/transition 改这些分支对应的工作项。
    # 留空表示「不限制」（生产慎用）。逗号分隔。
    sync_branch_whitelist: str = "heikesong_test"

    # --- Sync (F5) ---
    sync_source_node_name: str = "功能开发"
    sync_target_node_names: str = "功能测试,提测,测试中"

    # --- Report ---
    report_window_days: int = 7

    # --- 全员扫描（--scope project / all 用）---
    # 当前 token 个人视角能拿的工作项由 list_todo 决定（默认 scope=mine）。
    # 切到 project / all 时改走 search_by_mql；需要这两项：
    meego_space_simple_name: str = ""  # 留空时 fetcher 会自动查；预填能省一次 API 调用
    meego_scan_types: str = "执行需求"  # 逗号分隔，按团队工作流改

    # --- Runtime paths ---
    data_dir: Path = field(default_factory=lambda: Path.cwd() / "data")

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "Config":
        _load_dotenv_if_present(env_file)
        return cls(
            meego_mcp_url=os.environ.get("MEEGO_MCP_URL", cls.meego_mcp_url),
            meego_mcp_token=os.environ.get("MEEGO_MCP_TOKEN", ""),
            meego_project_key=os.environ.get("MEEGO_PROJECT_KEY", ""),
            meego_report_carrier_id=os.environ.get("MEEGO_REPORT_CARRIER_ID", ""),
            meego_report_carrier_type_key=os.environ.get(
                "MEEGO_REPORT_CARRIER_TYPE_KEY", ""
            ),
            meego_focus_work_item_id=os.environ.get("MEEGO_FOCUS_WORK_ITEM_ID", ""),
            git_provider=os.environ.get("GIT_PROVIDER", "local"),
            local_git_repo_path=os.environ.get("LOCAL_GIT_REPO_PATH", "."),
            local_git_repo_root=os.environ.get("LOCAL_GIT_REPO_ROOT", ""),
            local_git_remote_prefix=os.environ.get(
                "LOCAL_GIT_REMOTE_PREFIX", "origin/"
            ),
            repo_id_map=os.environ.get("REPO_ID_MAP", ""),
            github_api_base=os.environ.get(
                "GITHUB_API_BASE", "https://api.github.com"
            ),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            github_default_repo=os.environ.get("GITHUB_DEFAULT_REPO", ""),
            gitlab_api_base=os.environ.get(
                "GITLAB_API_BASE", "https://git.ziniao.com/api/v4"
            ),
            gitlab_token=os.environ.get("GITLAB_TOKEN", ""),
            gitlab_default_project=os.environ.get("GITLAB_DEFAULT_PROJECT", ""),
            merge_target_branches=os.environ.get("MERGE_TARGET_BRANCHES", "test"),
            sync_source_node_name=os.environ.get("SYNC_SOURCE_NODE_NAME", "功能开发"),
            sync_target_node_names=os.environ.get(
                "SYNC_TARGET_NODE_NAMES", "功能测试,提测,测试中"
            ),
            sync_branch_whitelist=os.environ.get(
                "SYNC_BRANCH_WHITELIST", "heikesong_test"
            ),
            report_window_days=int(os.environ.get("REPORT_WINDOW_DAYS", "7")),
            meego_space_simple_name=os.environ.get("MEEGO_SPACE_SIMPLE_NAME", ""),
            meego_scan_types=os.environ.get("MEEGO_SCAN_TYPES", "执行需求"),
        )

    @property
    def merge_target_branch_list(self) -> list:
        return [b.strip() for b in self.merge_target_branches.split(",") if b.strip()]

    @property
    def sync_target_node_name_list(self) -> list:
        return [n.strip() for n in self.sync_target_node_names.split(",") if n.strip()]

    @property
    def sync_branch_whitelist_list(self) -> list:
        return [
            b.strip() for b in self.sync_branch_whitelist.split(",") if b.strip()
        ]

    @property
    def scan_type_list(self) -> list:
        return [t.strip() for t in (self.meego_scan_types or "").split(",") if t.strip()]

    @property
    def repo_id_map_dict(self) -> dict:
        """解析 REPO_ID_MAP="&abc=name1,&def=name2" 成 dict。"""
        out: dict = {}
        for piece in (self.repo_id_map or "").split(","):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            k, _, v = piece.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
        return out

    def resolve_repo_paths(self, repo_ids: list) -> list:
        """把工作项的 repos 字段（short code 列表）映射到本地仓库路径列表。

        优先级：REPO_ID_MAP > LOCAL_GIT_REPO_ROOT/{id} > LOCAL_GIT_REPO_PATH（兜底）。
        返回 (path_str, label) 二元组列表，label 用于展示。
        """
        from pathlib import Path as _P

        results = []
        seen = set()
        m = self.repo_id_map_dict
        root = self.local_git_repo_root.strip()

        for rid in repo_ids:
            rid_norm = rid.strip()
            if not rid_norm:
                continue
            # 1) 命中映射表
            mapped = m.get(rid_norm)
            # 2) 容器目录 + short code（直接当目录名）
            if not mapped and root:
                candidate = _P(root) / rid_norm
                if (candidate / ".git").exists():
                    mapped = str(candidate)
            if not mapped:
                continue
            path = mapped
            if root and not _P(path).is_absolute():
                path = str(_P(root) / path)
            if path in seen:
                continue
            seen.add(path)
            results.append((path, _P(path).name))

        if not results and self.local_git_repo_path:
            results.append((self.local_git_repo_path, _P(self.local_git_repo_path).name))
        return results

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    def require_meego(self) -> None:
        missing = []
        if not self.meego_mcp_token:
            missing.append("MEEGO_MCP_TOKEN")
        if not self.meego_project_key:
            missing.append("MEEGO_PROJECT_KEY")
        if missing:
            raise RuntimeError(
                "缺少必需的飞书项目配置: " + ", ".join(missing) + "（请检查 .env）"
            )
