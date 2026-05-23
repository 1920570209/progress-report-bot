"""数据契约：所有跨模块的 schema 都在这里定义，外部数据先映射到这里再使用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# ============================================================
# 飞书项目侧
# ============================================================

@dataclass
class Person:
    user_key: str
    name: str
    email: str = ""

    @classmethod
    def from_meego(cls, raw: dict) -> "Person":
        return cls(
            user_key=str(raw.get("user_key") or raw.get("key") or ""),
            name=str(raw.get("name") or ""),
            email=str(raw.get("email") or ""),
        )


@dataclass
class NodeSchedule:
    estimate_start: Optional[datetime] = None
    estimate_finish: Optional[datetime] = None
    actual_begin: Optional[datetime] = None
    actual_finish: Optional[datetime] = None
    is_delayed: bool = False
    points: Optional[float] = None
    actual_work_time: Optional[float] = None


@dataclass
class WorkItemNode:
    """工作项的一个流程节点（如 "功能开发"）。"""
    node_key: str
    node_name: str
    status: str  # "doing" | "finished" | "未开始" ...
    owners: List[Person] = field(default_factory=list)
    schedule: NodeSchedule = field(default_factory=NodeSchedule)
    branch: Optional[str] = None
    repos: List[str] = field(default_factory=list)


@dataclass
class WorkItem:
    project_key: str
    project_name: str
    work_item_id: str
    work_item_name: str
    work_item_type_key: str
    current_node_name: str = ""
    current_node_status: str = ""
    owners: List[Person] = field(default_factory=list)
    nodes: List[WorkItemNode] = field(default_factory=list)
    is_delayed: bool = False
    branch: Optional[str] = None
    repos: List[str] = field(default_factory=list)
    url: str = ""

    @property
    def primary_owner(self) -> Optional[Person]:
        return self.owners[0] if self.owners else None


# ============================================================
# GitHub 侧
# ============================================================

@dataclass
class GitCommit:
    sha: str
    message: str
    author: str
    date: datetime
    url: str = ""


@dataclass
class PullRequest:
    number: int
    title: str
    state: str  # "open" | "closed" | "merged"
    author: str
    head_branch: str
    base_branch: str
    url: str = ""
    merged: bool = False


@dataclass
class BranchActivity:
    """单个分支在时间窗口内的活动汇总。"""
    repo: str
    branch: str
    commits: List[GitCommit] = field(default_factory=list)
    pull_requests: List[PullRequest] = field(default_factory=list)
    exists: bool = True

    @property
    def commit_count(self) -> int:
        return len(self.commits)

    @property
    def open_pr_count(self) -> int:
        return sum(1 for p in self.pull_requests if p.state == "open" and not p.merged)

    @property
    def merged_pr_count(self) -> int:
        return sum(1 for p in self.pull_requests if p.merged)


# ============================================================
# 聚合视图
# ============================================================

@dataclass
class WorkItemEnriched:
    """工作项 + 该工作项分支上的 GitHub 活动（可能为空）。"""
    work_item: WorkItem
    git: Optional[BranchActivity] = None


@dataclass
class ProjectSnapshot:
    """F1 fetcher 的输出 = F2 analyzer 的输入。"""
    project_key: str
    project_name: str
    fetched_at: datetime
    window_days: int
    todo_items: List[WorkItem] = field(default_factory=list)
    done_items: List[WorkItem] = field(default_factory=list)
    enriched: List[WorkItemEnriched] = field(default_factory=list)


# ============================================================
# 报告
# ============================================================

@dataclass
class Risk:
    work_item: WorkItem
    reason: str
    severity: str = "warning"  # "critical" | "warning" | "info"


@dataclass
class OwnerLine:
    owner: Person
    todo_count: int = 0
    done_count: int = 0
    delayed_count: int = 0
    commit_count: int = 0


@dataclass
class ProgressLine:
    """一条「需求进度」明细，用于「📋 本周需求进度」段。"""
    work_item: WorkItem
    headline: str             # 一句话进度（节点 + 负责人 + 状态）
    detail: List[str] = field(default_factory=list)  # 副信号（commits/MR/延期…）
    git: Optional[BranchActivity] = None
    is_delayed: bool = False


@dataclass
class ProgressView:
    completed: List[ProgressLine] = field(default_factory=list)   # 🚀 本周完成节点
    active: List[ProgressLine] = field(default_factory=list)      # 🏃 推进中
    pending_count: int = 0                                        # 📥 待启动总数
    pending_sample: List[ProgressLine] = field(default_factory=list)


@dataclass
class ReportData:
    """F2 analyzer 的输出 = F3 pusher / renderer 的输入。"""
    project_name: str
    window_days: int
    generated_at: datetime
    summary_oneline: str
    total_count: int
    done_count: int
    completion_rate: float  # 0.0 ~ 1.0
    delayed_items: List[WorkItem] = field(default_factory=list)
    risks: List[Risk] = field(default_factory=list)
    owner_lines: List[OwnerLine] = field(default_factory=list)
    git_total_commits: int = 0
    git_total_prs_merged: int = 0
    git_items: List[WorkItemEnriched] = field(default_factory=list)
    mentions: List[Person] = field(default_factory=list)
    progress: Optional[ProgressView] = None  # 本周需求进度明细
    diff: Optional["DiffReport"] = None      # F6 飞书↔Git 对账（可选）


# ============================================================
# F6 差异/对账
# ============================================================

# Discrepancy.kind 取值：
#   "fake_done"        飞书已完成但 Git 无 merged 证据
#   "lag"              Git 已 merged 但飞书还停在「功能开发」
#   "lead"             飞书已推进到测试/上线，但 Git 还没 merged
#   "missing_branch"   处于开发节点但「开发分支」字段为空
#   "branch_not_found" 已填分支，但 GitLab 上不存在
#   "stale_branch"     分支存在但窗口内 0 commit
#   "no_repo"          已填分支但缺仓库配置
DiscrepancyKind = str  # 用 str 而非 Enum 便于 dataclass.asdict 序列化


@dataclass
class Discrepancy:
    """一条飞书 ↔ Git 不一致项。"""
    kind: DiscrepancyKind
    severity: str  # "critical" | "warning" | "info"
    work_item: WorkItem
    title: str         # 一行可读摘要（演示用）
    detail: str = ""   # 多行明细（详情用）
    feishu_signal: str = ""   # 飞书侧观察到的信号
    git_signal: str = ""      # Git 侧观察到的信号
    suggested_action: str = ""
    git: Optional["BranchActivity"] = None


@dataclass
class DiffReport:
    """飞书项目 vs Git 实际状态的对账报告。"""
    project_name: str
    generated_at: datetime
    window_days: int
    scanned_total: int = 0
    scanned_with_branch: int = 0
    scanned_with_git: int = 0
    discrepancies: List[Discrepancy] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.severity == "info")

    def by_kind(self, kind: str) -> List[Discrepancy]:
        return [d for d in self.discrepancies if d.kind == kind]
