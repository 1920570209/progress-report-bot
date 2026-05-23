"""F2 分析：ProjectSnapshot → ReportData。

设计原则：
- 纯函数式：不调外部 API，不写文件
- 规则透明：所有阈值 / 排序逻辑都明文写在常量里
- 老板视角：粒度粗、可读性高，不堆细节
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from ..config import Config
from ..models import (
    BranchActivity,
    DiffReport,
    OwnerLine,
    Person,
    ProgressLine,
    ProgressView,
    ProjectSnapshot,
    ReportData,
    Risk,
    WorkItem,
    WorkItemEnriched,
)
from .diff_analyzer import DiffAnalyzer

logger = logging.getLogger(__name__)


# ============================================================
# 可调阈值
# ============================================================

CRITICAL_DELAY_DAYS = 3            # 延期 > N 天 → critical
WARNING_NO_BRANCH_NODES = {        # 处于这些节点却没有开发分支 → warning
    "功能开发",
    "提测",
    "功能测试",
}
TOP_RISKS_LIMIT = 5
TOP_OWNERS_LIMIT = 10

# 进度分类规则
PENDING_NODE_NAMES = {"需求已创建（待处理）", "需求已创建", "待处理"}
ACTIVE_NODE_NAMES = {"功能开发", "提测", "功能测试", "测试中", "showcase"}
PROGRESS_ACTIVE_LIMIT = 8        # 推进中段最多展示几条
PROGRESS_PENDING_SAMPLE = 3      # 待启动段最多展示几条


class Analyzer:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg

    def analyze(
        self,
        snapshot: ProjectSnapshot,
        *,
        with_diff: bool = True,
    ) -> ReportData:
        now = datetime.now()

        todo = snapshot.todo_items
        done = snapshot.done_items
        total = len(todo) + len(done)
        completion_rate = (len(done) / total) if total else 0.0

        delayed_items = [w for w in todo if w.is_delayed]
        risks = self._build_risks(todo, now)
        owner_lines = self._aggregate_owners(todo, done)
        mentions = self._collect_mentions(delayed_items, done)
        summary = self._build_summary(
            project_name=snapshot.project_name,
            window_days=snapshot.window_days,
            done_count=len(done),
            total_count=total,
            completion_rate=completion_rate,
            risk_count=len(risks),
        )

        git_items = [
            e for e in snapshot.enriched if e.git is not None
        ]
        git_total_commits = sum(e.git.commit_count for e in git_items if e.git)
        git_total_prs_merged = sum(
            e.git.merged_pr_count for e in git_items if e.git
        )

        diff_report: Optional[DiffReport] = None
        if with_diff and self.cfg is not None:
            diff_report = DiffAnalyzer(self.cfg).analyze(snapshot)

        progress_view = self._build_progress_view(snapshot)

        return ReportData(
            project_name=snapshot.project_name,
            window_days=snapshot.window_days,
            generated_at=now,
            summary_oneline=summary,
            total_count=total,
            done_count=len(done),
            completion_rate=completion_rate,
            delayed_items=delayed_items,
            risks=risks,
            owner_lines=owner_lines,
            git_total_commits=git_total_commits,
            git_total_prs_merged=git_total_prs_merged,
            git_items=git_items,
            mentions=mentions,
            progress=progress_view,
            diff=diff_report,
        )

    # --------------------------------------------------------
    # 规则块
    # --------------------------------------------------------

    def _build_risks(self, todo: List[WorkItem], now: datetime) -> List[Risk]:
        raised: List[Risk] = []
        for w in todo:
            if w.is_delayed:
                days = self._delay_days(w, now)
                reason = self._format_delay_reason(days)
                severity = (
                    "critical"
                    if (days is not None and days >= CRITICAL_DELAY_DAYS)
                    else "warning"
                )
                raised.append(Risk(work_item=w, reason=reason, severity=severity))
                continue
            if (
                w.current_node_name in WARNING_NO_BRANCH_NODES
                and not w.branch
            ):
                raised.append(
                    Risk(
                        work_item=w,
                        reason=f"处于「{w.current_node_name}」节点但未填开发分支",
                        severity="warning",
                    )
                )
        raised.sort(key=lambda r: {"critical": 0, "warning": 1, "info": 2}[r.severity])
        return raised[:TOP_RISKS_LIMIT]

    @staticmethod
    def _delay_days(w: WorkItem, now: datetime) -> Optional[int]:
        for n in w.nodes:
            if n.status == "doing" and n.schedule.estimate_finish:
                delta = now - n.schedule.estimate_finish
                if delta.total_seconds() > 0:
                    return delta.days
        return None

    @staticmethod
    def _format_delay_reason(days: Optional[int]) -> str:
        if days is None:
            return "已标记为延期"
        if days <= 0:
            return "今日到期但未完成"
        return f"已延期 {days} 天"

    @staticmethod
    def _aggregate_owners(
        todo: List[WorkItem], done: List[WorkItem]
    ) -> List[OwnerLine]:
        index: "OrderedDict[str, OwnerLine]" = OrderedDict()

        def _bump(person: Optional[Person], **kwargs):
            if person is None or not person.user_key:
                return
            line = index.get(person.user_key)
            if line is None:
                line = OwnerLine(owner=person)
                index[person.user_key] = line
            for k, v in kwargs.items():
                setattr(line, k, getattr(line, k) + v)

        for w in todo:
            _bump(w.primary_owner, todo_count=1)
            if w.is_delayed:
                _bump(w.primary_owner, delayed_count=1)
        for w in done:
            _bump(w.primary_owner, done_count=1)

        lines = list(index.values())
        lines.sort(
            key=lambda l: (-l.done_count, -l.delayed_count, -l.todo_count)
        )
        return lines[:TOP_OWNERS_LIMIT]

    @staticmethod
    def _collect_mentions(
        delayed: List[WorkItem], done: List[WorkItem]
    ) -> List[Person]:
        seen: Dict[str, Person] = OrderedDict()
        for w in delayed + done:
            p = w.primary_owner
            if p and p.user_key and p.user_key not in seen:
                seen[p.user_key] = p
        return list(seen.values())

    def _build_progress_view(self, snapshot: ProjectSnapshot) -> ProgressView:
        """生成「📋 本周需求进度」三分组视图。"""
        git_by_id = {
            e.work_item.work_item_id: e.git
            for e in snapshot.enriched
            if e.git is not None
        }
        done_ids = {w.work_item_id for w in snapshot.done_items}

        view = ProgressView()

        # 🚀 本周完成节点（done_items：本周有节点完成事件）
        # 同一工作项可能在 done 与 todo 同时出现（节点流转），这里以 done 为准
        for w in snapshot.done_items:
            view.completed.append(
                self._make_progress_line(
                    w,
                    git=git_by_id.get(w.work_item_id),
                    phase="completed",
                )
            )

        # 🏃 推进中 / 📥 待启动（来自 todo_items，但排除 done 集合避免重复）
        pending: List[ProgressLine] = []
        active: List[ProgressLine] = []
        for w in snapshot.todo_items:
            if w.work_item_id in done_ids:
                continue
            line = self._make_progress_line(
                w,
                git=git_by_id.get(w.work_item_id),
                phase="active" if w.current_node_name not in PENDING_NODE_NAMES else "pending",
            )
            if w.current_node_name in PENDING_NODE_NAMES:
                pending.append(line)
            else:
                active.append(line)

        # 推进中：延期优先 → 有 git 活动优先 → 节点名
        active.sort(
            key=lambda l: (
                0 if l.work_item.is_delayed else 1,
                0 if l.git and (l.git.commit_count or l.git.pull_requests) else 1,
                l.work_item.current_node_name,
            )
        )
        view.active = active[:PROGRESS_ACTIVE_LIMIT]

        view.pending_count = len(pending)
        view.pending_sample = pending[:PROGRESS_PENDING_SAMPLE]
        return view

    @staticmethod
    def _make_progress_line(
        w: WorkItem,
        git: Optional[BranchActivity],
        phase: str,
    ) -> ProgressLine:
        owner_name = w.primary_owner.name if w.primary_owner else "—"
        status_zh = {"doing": "进行中", "finished": "已完成"}.get(
            w.current_node_status, w.current_node_status or "—"
        )

        if phase == "completed":
            verb = "本周已推进至" if w.current_node_status == "doing" else "本周已完成"
            headline = (
                f"{verb}「{w.current_node_name}」 · {owner_name}"
            )
        elif phase == "active":
            headline = (
                f"「{w.current_node_name}」{status_zh} · {owner_name}"
                + (" · ⏰延期" if w.is_delayed else "")
            )
        else:  # pending
            headline = f"「{w.current_node_name}」 · {owner_name}（未启动）"

        detail: List[str] = []
        if w.branch:
            detail.append(f"分支 `{w.branch}`")
        if git is not None:
            if not git.exists:
                detail.append("⚠ 分支在 Git 不存在")
            else:
                if git.commit_count:
                    detail.append(f"本周 {git.commit_count} commits")
                if git.merged_pr_count:
                    detail.append(f"已合并 {git.merged_pr_count} MR")
                if git.open_pr_count:
                    detail.append(f"待合 {git.open_pr_count} MR")
                if (
                    not git.commit_count
                    and not git.merged_pr_count
                    and not git.open_pr_count
                ):
                    detail.append("窗口内无代码活动")

        return ProgressLine(
            work_item=w,
            headline=headline,
            detail=detail,
            git=git,
            is_delayed=w.is_delayed,
        )

    @staticmethod
    def _build_summary(
        *,
        project_name: str,
        window_days: int,
        done_count: int,
        total_count: int,
        completion_rate: float,
        risk_count: int,
    ) -> str:
        pct = int(round(completion_rate * 100))
        if total_count == 0:
            return f"{project_name} 最近 {window_days} 天暂无活跃工作项"
        pieces = [
            f"近 {window_days} 天：完成 {done_count}/{total_count} 项（{pct}%）"
        ]
        if risk_count:
            pieces.append(f"⚠ 发现 {risk_count} 项风险")
        else:
            pieces.append("无明显风险")
        return "，".join(pieces) + "。"
