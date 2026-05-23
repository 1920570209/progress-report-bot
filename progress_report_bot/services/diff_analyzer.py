"""F6 飞书 ↔ Git 对账分析。

输入：``ProjectSnapshot``（含 enriched git 数据 + meego work items）
输出：``DiffReport``（一组 ``Discrepancy``）

规则汇总（按严重度从高到低）：

| kind                | 触发条件 | severity |
|---------------------|----------|----------|
| ``fake_done``       | 飞书 done + 节点完成 + 节点∈测试相关 + Git 0 merged MR 到 target | critical |
| ``lag``             | 飞书 doing「功能开发」 + Git 已 merged MR 到 target | critical |
| ``stagnant_node``   | doing 节点 actual_begin 到现在 > 14 天且无 actual_finish（纯飞书）| critical |
| ``overdue``         | doing 节点 estimate_finish 超期 > 0（纯飞书）| critical/warning |
| ``branch_not_found``| 飞书填了分支 + GitLab/Git 上不存在该分支 | warning |
| ``lead``            | 飞书 doing「功能测试/提测」 + Git 没有 merged MR | warning |
| ``missing_branch``  | 飞书 doing「功能开发/提测/功能测试」 + 未填分支 | warning |
| ``no_repo``         | 已填分支但 repo 无法解析（DEFAULT_PROJECT 为空且工作项也没填）| warning |
| ``stale_branch``    | 分支存在但窗口内 0 commit + 0 merged MR | info |

**纯飞书模式**（``GIT_PROVIDER=none`` 或所有工作项无 branch）：仅 ``stagnant_node`` /
``overdue`` 规则生效，``missing_branch`` / ``no_repo`` 被自动屏蔽。

设计原则：
- 纯函数：不调外部 API，只消费 snapshot 已有数据
- 可解释：每条 Discrepancy 都给出 ``feishu_signal`` / ``git_signal`` / ``suggested_action``
- 可演示：title 是"老板能看懂"的一句话
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from ..config import Config
from ..models import (
    BranchActivity,
    Discrepancy,
    DiffReport,
    ProjectSnapshot,
    WorkItem,
    WorkItemEnriched,
)
from .git_factory import resolve_repo

logger = logging.getLogger(__name__)


# 节点名归类
DEV_NODES = {"功能开发"}
TEST_NODES = {"提测", "功能测试", "测试中"}
RELEASE_NODES = {"上线", "已上线", "发布"}

STAGNANT_DAYS_THRESHOLD = 30   # doing 节点超过 N 天无完成 → warning
STAGNANT_DAYS_CRITICAL = 90    # 超过 N 天 → critical


class DiffAnalyzer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.targets = cfg.merge_target_branch_list

    def analyze(self, snapshot: ProjectSnapshot) -> DiffReport:
        report = DiffReport(
            project_name=snapshot.project_name,
            generated_at=datetime.now(),
            window_days=snapshot.window_days,
        )

        enriched_by_id = {e.work_item.work_item_id: e for e in snapshot.enriched}
        done_ids = {w.work_item_id for w in snapshot.done_items}

        report.scanned_total = len(enriched_by_id)
        report.scanned_with_branch = sum(
            1 for e in enriched_by_id.values() if e.work_item.branch
        )
        report.scanned_with_git = sum(
            1 for e in enriched_by_id.values() if e.git is not None
        )

        # 模式判定：若整批没有 git 数据，进入"纯飞书模式"
        # 此时屏蔽 git 相关规则（fake_done/lag/branch_not_found/missing_branch/no_repo/stale_branch）
        pure_feishu_mode = report.scanned_with_git == 0

        for wid, e in enriched_by_id.items():
            w = e.work_item
            is_done = wid in done_ids

            # 纯飞书规则（始终生效，不依赖 git）
            for d in self._diff_one_feishu(w, is_done):
                report.discrepancies.append(d)

            # Git 相关规则（仅当有 git 数据时）
            if not pure_feishu_mode:
                for d in self._diff_one(w, e.git, is_done):
                    report.discrepancies.append(d)

        # 排序：critical > warning > info；同级按工作项 id
        sev_rank = {"critical": 0, "warning": 1, "info": 2}
        report.discrepancies.sort(
            key=lambda d: (sev_rank.get(d.severity, 9), d.work_item.work_item_id)
        )
        return report

    # ------------------------------------------------------------
    # 纯飞书规则（不依赖 git）
    # ------------------------------------------------------------

    def _diff_one_feishu(
        self,
        w: WorkItem,
        is_done: bool,
    ) -> List[Discrepancy]:
        """纯飞书侧的一致性检查 —— stagnant_node / overdue。"""
        out: List[Discrepancy] = []
        now = datetime.now()
        if is_done or not w.nodes:
            return out

        # 找当前 doing 节点
        doing = next((n for n in w.nodes if n.status == "doing"), None)
        if doing is None:
            return out

        sched = doing.schedule
        # 规则 A: overdue —— estimate_finish 已过
        if sched.estimate_finish and sched.estimate_finish < now and not sched.actual_finish:
            days = (now - sched.estimate_finish).days
            sev = "critical" if days >= 3 else "warning"
            out.append(
                Discrepancy(
                    kind="overdue",
                    severity=sev,
                    work_item=w,
                    title=f"#{w.work_item_id} 节点「{doing.node_name}」已超期 {days} 天",
                    feishu_signal=(
                        f"estimate_finish={sched.estimate_finish.strftime('%Y-%m-%d')}"
                        f" actual_finish=空"
                    ),
                    git_signal="—",
                    suggested_action="确认是否能完成；如不能，调整排期或转交",
                )
            )

        # 规则 B: stagnant_node —— actual_begin 距今 > N 天且无 actual_finish
        if (
            sched.actual_begin
            and not sched.actual_finish
            and (now - sched.actual_begin).days >= STAGNANT_DAYS_THRESHOLD
        ):
            days = (now - sched.actual_begin).days
            out.append(
                Discrepancy(
                    kind="stagnant_node",
                    severity="critical" if days >= STAGNANT_DAYS_CRITICAL else "warning",
                    work_item=w,
                    title=f"#{w.work_item_id} 节点「{doing.node_name}」停滞 {days} 天无完成",
                    feishu_signal=(
                        f"actual_begin={sched.actual_begin.strftime('%Y-%m-%d')}"
                        f" actual_finish=空"
                    ),
                    git_signal="—",
                    suggested_action="跟进负责人，必要时拆分或重新分配",
                )
            )
        return out

    # ------------------------------------------------------------
    # Git 相关规则集
    # ------------------------------------------------------------

    def _diff_one(
        self,
        w: WorkItem,
        git: Optional[BranchActivity],
        is_done: bool,
    ) -> List[Discrepancy]:
        out: List[Discrepancy] = []
        node = w.current_node_name or ""
        status = w.current_node_status or ""
        has_merged = self._has_merged_to_targets(git)

        # 规则 1: fake_done —— 飞书 done + 节点完成 + Git 无 merged 证据
        if (
            is_done
            and status == "finished"
            and node in DEV_NODES | TEST_NODES | RELEASE_NODES
            and w.branch
            and git is not None
            and git.exists
            and not has_merged
            and not git.commits
        ):
            out.append(
                Discrepancy(
                    kind="fake_done",
                    severity="critical",
                    work_item=w,
                    title=f"#{w.work_item_id} 飞书已完成「{node}」，但代码无任何活动",
                    feishu_signal=f"节点「{node}」status=finished",
                    git_signal=f"分支 {w.branch} 近 {self.cfg.report_window_days} 天 0 commit、0 merged MR",
                    suggested_action="确认是否漏推代码 / 修复飞书状态",
                    git=git,
                )
            )
            return out  # 该工作项 critical 已命中，不再产生其他维度

        # 规则 2: lag —— 飞书还在「功能开发」/doing，但 Git 已合并
        if (
            not is_done
            and node in DEV_NODES
            and status == "doing"
            and has_merged
        ):
            target = self._merged_target(git)
            out.append(
                Discrepancy(
                    kind="lag",
                    severity="critical",
                    work_item=w,
                    title=f"#{w.work_item_id} 代码已合并到 {target}，但飞书仍停在「{node}」",
                    feishu_signal=f"节点「{node}」status=doing",
                    git_signal=f"分支 {w.branch} 已 merged 到 {target}",
                    suggested_action="执行 `progress-report-bot sync --apply` 自动推进",
                    git=git,
                )
            )
            return out

        # 规则 3: missing_branch —— 该填分支但没填
        if (
            not is_done
            and status == "doing"
            and node in DEV_NODES | TEST_NODES
            and not w.branch
        ):
            out.append(
                Discrepancy(
                    kind="missing_branch",
                    severity="warning",
                    work_item=w,
                    title=f"#{w.work_item_id} 处于「{node}」节点但未填「开发分支」",
                    feishu_signal=f"节点「{node}」status=doing；field_1946d0=空",
                    git_signal="—",
                    suggested_action="去飞书工作项「开发分支」字段补填",
                )
            )
            # 不 return，继续看是否还有其它问题

        # 规则 4: branch_not_found —— 填了分支但 GitLab 上不存在
        if w.branch and git is not None and not git.exists:
            out.append(
                Discrepancy(
                    kind="branch_not_found",
                    severity="warning",
                    work_item=w,
                    title=f"#{w.work_item_id} 飞书填的分支 {w.branch} 在 Git 不存在",
                    feishu_signal=f"field_1946d0 = {w.branch}",
                    git_signal="该分支在 Git 仓库中查无此 ref",
                    suggested_action="核对分支名拼写或创建分支",
                    git=git,
                )
            )
            return out

        # 规则 5: no_repo —— 填了分支但没仓库配置
        if w.branch and git is None and self._no_repo_resolved(w):
            out.append(
                Discrepancy(
                    kind="no_repo",
                    severity="warning",
                    work_item=w,
                    title=f"#{w.work_item_id} 填了分支 {w.branch} 但缺仓库配置",
                    feishu_signal=f"field_1946d0 = {w.branch}；「选择仓库」字段为空",
                    git_signal="未触发 Git enrich（无 repo 路径）",
                    suggested_action="在飞书工作项「选择仓库」补填，或在 .env 配 GITLAB_DEFAULT_PROJECT",
                )
            )
            return out

        # 规则 6: lead —— 飞书已推进到测试/提测节点，但 Git 还没合并
        if (
            not is_done
            and node in TEST_NODES
            and status == "doing"
            and w.branch
            and git is not None
            and git.exists
            and not has_merged
        ):
            out.append(
                Discrepancy(
                    kind="lead",
                    severity="warning",
                    work_item=w,
                    title=f"#{w.work_item_id} 飞书已到「{node}」，但代码还没合到 {self.targets}",
                    feishu_signal=f"节点「{node}」status=doing",
                    git_signal=f"分支 {w.branch} 0 merged MR 到目标",
                    suggested_action="确认提测是否已实际合并，或回退飞书状态",
                    git=git,
                )
            )

        # 规则 7: stale_branch —— 有分支但窗口内 0 活动
        if (
            not is_done
            and w.branch
            and git is not None
            and git.exists
            and git.commit_count == 0
            and not git.pull_requests
            and not out  # 没有更严重的命中
        ):
            out.append(
                Discrepancy(
                    kind="stale_branch",
                    severity="info",
                    work_item=w,
                    title=f"#{w.work_item_id} 分支 {w.branch} 近 {self.cfg.report_window_days} 天无提交",
                    feishu_signal=f"节点「{node}」status={status}",
                    git_signal=f"分支存在但窗口内 0 commit、0 MR",
                    suggested_action="确认是否在做、是否需要拆解",
                    git=git,
                )
            )

        return out

    # ------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------

    def _has_merged_to_targets(self, git: Optional[BranchActivity]) -> bool:
        if git is None or not git.pull_requests or not self.targets:
            return False
        tset = {t.lower() for t in self.targets}
        return any(
            p.merged and (p.base_branch or "").lower() in tset
            for p in git.pull_requests
        )

    def _merged_target(self, git: Optional[BranchActivity]) -> str:
        if git is None:
            return ""
        for p in git.pull_requests:
            if p.merged and (p.base_branch or "").lower() in {
                t.lower() for t in self.targets
            }:
                return p.base_branch
        return ""

    def _no_repo_resolved(self, w: WorkItem) -> bool:
        return not resolve_repo(self.cfg, w)


# ----------------------------------------------------------------
# 终端 / 文本格式化
# ----------------------------------------------------------------

KIND_LABELS = {
    "fake_done": "疑似假完成",
    "lag": "状态滞后",
    "stagnant_node": "节点停滞",
    "overdue": "节点超期",
    "branch_not_found": "分支不存在",
    "lead": "状态领先",
    "missing_branch": "未填分支",
    "no_repo": "缺仓库配置",
    "stale_branch": "分支僵死",
}

SEV_ICON = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


def format_diff_terminal(report: DiffReport) -> str:
    lines = [
        f"项目: {report.project_name}",
        f"扫描: total={report.scanned_total} | 带分支={report.scanned_with_branch} | 已 enrich={report.scanned_with_git}",
        f"差异: {len(report.discrepancies)} 项 "
        f"(🔴 {report.critical_count} | 🟡 {report.warning_count} | 🔵 {report.info_count})",
        "",
    ]
    if not report.discrepancies:
        lines.append("✅ 飞书状态与 Git 实际一致，无差异")
        return "\n".join(lines)

    for d in report.discrepancies:
        lines.append(f"{SEV_ICON.get(d.severity,'•')} [{KIND_LABELS.get(d.kind, d.kind)}] {d.title}")
        if d.feishu_signal:
            lines.append(f"    feishu : {d.feishu_signal}")
        if d.git_signal:
            lines.append(f"    git    : {d.git_signal}")
        if d.suggested_action:
            lines.append(f"    建议   : {d.suggested_action}")
        owner = d.work_item.primary_owner
        if owner:
            lines.append(f"    负责人 : {owner.name}")
        lines.append("")
    return "\n".join(lines)
