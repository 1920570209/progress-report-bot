"""F5 双向同步：Git MR/PR 已合并到测试分支 → 飞书项目节点自动流转。

默认 dry-run：只输出「将会流转」清单，不调用 transition_node。
加 ``--apply`` 才会真实改状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ..config import Config
from ..models import WorkItem
from .fetcher import Fetcher
from .git_factory import make_git_client, resolve_repo
from .meego_client import MeegoClient, MeegoMCPError

logger = logging.getLogger(__name__)


@dataclass
class SyncCandidate:
    work_item: WorkItem
    repo: str
    branch: str
    pr_number: int
    pr_url: str
    pr_base: str
    node_key: str
    target_node_hint: str = ""
    block_reason: str = ""
    applied: bool = False
    apply_result: str = ""


@dataclass
class SyncResult:
    scanned: int = 0
    candidates: List[SyncCandidate] = field(default_factory=list)
    skipped: List[SyncCandidate] = field(default_factory=list)
    dry_run: bool = True


class SyncService:
    SOURCE_NODE = None  # 从 cfg 读

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.meego = MeegoClient(cfg.meego_mcp_url, cfg.meego_mcp_token)
        self.git = make_git_client(cfg)
        self.fetcher = Fetcher(cfg)

    def run(self, *, apply: bool = False) -> SyncResult:
        self.cfg.require_meego()
        if not self.git.enabled:
            raise RuntimeError(
                "Git provider 已禁用（GIT_PROVIDER={}），sync 功能需要 git 数据。\n"
                "请在 .env 把 GIT_PROVIDER 改成 local / gitlab / github 之一并配置。".format(
                    self.cfg.git_provider or "(空)"
                )
            )

        snapshot = self.fetcher.fetch(persist=False)
        source_node = self.cfg.sync_source_node_name
        targets = self.cfg.merge_target_branch_list
        whitelist = self.cfg.sync_branch_whitelist_list
        result = SyncResult(dry_run=not apply)

        todo_items = snapshot.todo_items
        focus = self.cfg.meego_focus_work_item_id.strip()
        if focus:
            todo_items = [w for w in todo_items if w.work_item_id == focus]
            logger.info("focus 模式：仅扫描工作项 #%s", focus)
        if whitelist:
            logger.info("分支白名单已启用，仅允许操作: %s", whitelist)
        else:
            logger.warning("⚠ 分支白名单为空（SYNC_BRANCH_WHITELIST），将对所有匹配分支生效")

        for w in todo_items:
            result.scanned += 1
            if w.current_node_name != source_node:
                continue
            if not w.branch:
                result.skipped.append(
                    SyncCandidate(
                        work_item=w,
                        repo="",
                        branch="",
                        pr_number=0,
                        pr_url="",
                        pr_base="",
                        node_key=self._current_node_key(w),
                        block_reason="未填写开发分支",
                    )
                )
                continue

            if whitelist and w.branch not in whitelist:
                result.skipped.append(
                    SyncCandidate(
                        work_item=w,
                        repo="",
                        branch=w.branch,
                        pr_number=0,
                        pr_url="",
                        pr_base="",
                        node_key=self._current_node_key(w),
                        block_reason=f"分支 {w.branch} 不在白名单 {whitelist}（演示护栏）",
                    )
                )
                continue

            repo = resolve_repo(self.cfg, w)
            if not repo:
                result.skipped.append(
                    SyncCandidate(
                        work_item=w,
                        repo="",
                        branch=w.branch,
                        pr_number=0,
                        pr_url="",
                        pr_base="",
                        node_key=self._current_node_key(w),
                        block_reason="未配置仓库（GITLAB_DEFAULT_PROJECT / GITHUB_DEFAULT_REPO 或工作项「选择仓库」字段）",
                    )
                )
                continue

            merged_pr = self.git.has_merged_to_targets(repo, w.branch, targets)
            if not merged_pr:
                result.skipped.append(
                    SyncCandidate(
                        work_item=w,
                        repo=repo,
                        branch=w.branch,
                        pr_number=0,
                        pr_url="",
                        pr_base="",
                        node_key=self._current_node_key(w),
                        block_reason=f"暂无 MR/PR 合并到 {targets}",
                    )
                )
                continue

            node_key = self._current_node_key(w)
            cand = SyncCandidate(
                work_item=w,
                repo=repo,
                branch=w.branch,
                pr_number=merged_pr.number,
                pr_url=merged_pr.url,
                pr_base=merged_pr.base_branch,
                node_key=node_key,
                target_node_hint=self._guess_next_node_name(w),
            )

            block = self._check_transition_blockers(w, node_key)
            if block:
                cand.block_reason = block
                result.skipped.append(cand)
                continue

            result.candidates.append(cand)

            if apply:
                if whitelist and w.branch not in whitelist:
                    cand.apply_result = (
                        f"❌ 拒绝流转：分支 {w.branch} 不在白名单 {whitelist}"
                    )
                    cand.applied = False
                else:
                    cand.apply_result = self._apply_transition(w, node_key)
                    cand.applied = "失败" not in cand.apply_result and "拒绝" not in cand.apply_result

        if apply:
            self._audit(result)
        return result

    @staticmethod
    def _current_node_key(w: WorkItem) -> str:
        for n in w.nodes:
            if n.status == "doing":
                return n.node_key
        return ""

    @staticmethod
    def _guess_next_node_name(w: WorkItem) -> str:
        names = [n.node_name for n in w.nodes]
        for hint in ("功能测试", "提测", "测试中", "showcase"):
            if hint in names:
                return hint
        return "下一节点"

    def _check_transition_blockers(self, w: WorkItem, node_key: str) -> str:
        if not node_key:
            return "无法确定当前 node_key"
        try:
            req = self.meego.get_transition_required(
                project_key=w.project_key,
                work_item_id=w.work_item_id,
                state_key=node_key,
                mode="unfinished",
            )
        except MeegoMCPError as e:
            return f"查询流转必填项失败: {e}"

        unfinished = []
        for key in ("list", "fields", "required_fields", "unfinished_list"):
            val = req.get(key)
            if isinstance(val, list) and val:
                unfinished = val
                break
        if unfinished:
            return f"存在 {len(unfinished)} 个未完成必填项，需手动提测"
        return ""

    def _apply_transition(self, w: WorkItem, node_key: str) -> str:
        try:
            self.meego.transition_node(
                project_key=w.project_key,
                work_item_id=w.work_item_id,
                node_id=node_key,
                action="confirm",
            )
            return f"✅ 已流转 #{w.work_item_id} 节点 {node_key}"
        except MeegoMCPError as e:
            return f"❌ 流转失败: {e}"

    def _audit(self, result: SyncResult) -> None:
        try:
            log_path = self.cfg.ensure_data_dir() / "sync_history.log"
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as f:
                for c in result.candidates:
                    f.write(
                        f"{ts} | applied={c.applied} | #{c.work_item.work_item_id} | "
                        f"branch={c.branch} | pr=#{c.pr_number} base={c.pr_base} | "
                        f"result={c.apply_result}\n"
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("写 sync 审计日志失败: %s", e)


def format_sync_report(result: SyncResult) -> str:
    lines = [
        f"扫描 todo: {result.scanned} 项",
        f"可流转: {len(result.candidates)} 项",
        f"跳过/阻塞: {len(result.skipped)} 项",
        f"模式: {'apply' if not result.dry_run else 'dry-run'}",
        "",
    ]
    if result.candidates:
        lines.append("=== 将流转（PR 已合并到测试分支）===")
        for c in result.candidates:
            w = c.work_item
            lines.append(
                f"  #{w.work_item_id} {w.work_item_name[:40]}\n"
                f"    节点: {w.current_node_name} → {c.target_node_hint}\n"
                f"    分支: {c.branch}  repo: {c.repo}\n"
                f"    PR: #{c.pr_number} merged → {c.pr_base}  {c.pr_url}"
            )
            if c.apply_result:
                lines.append(f"    结果: {c.apply_result}")
            lines.append("")
    if result.skipped:
        lines.append("=== 跳过 ===")
        for c in result.skipped[:10]:
            w = c.work_item
            lines.append(
                f"  #{w.work_item_id} {w.work_item_name[:30]} — {c.block_reason or '无合并 PR'}"
            )
    if not result.candidates and not result.skipped:
        lines.append("（无处于「功能开发」且 PR 已合并到测试分支的工作项）")
    return "\n".join(lines)
