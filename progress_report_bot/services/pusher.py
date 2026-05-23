"""F3 推送：ReportData → 飞书工作项评论 + @负责人。

策略：
- 用 MCP add_comment（评论是富文本 markdown，飞书会自动渲染）
- @ 用飞书自定义语法 ``<at id="user_key">@姓名</at>``（已由 lark_comment.md.j2 模板生成）
- 默认行为：若 ``MEEGO_REPORT_CARRIER_ID`` 为空 ⇒ 自动 dry-run（只打印不推送）
- 真实推送后写一行到 ``data/push_history.log`` 作为审计
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import ReportData
from .meego_client import MeegoClient, MeegoMCPError
from .renderer import render_lark_comment

logger = logging.getLogger(__name__)


class Pusher:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.meego = MeegoClient(cfg.meego_mcp_url, cfg.meego_mcp_token)

    def push(
        self,
        report: ReportData,
        dry_run: Optional[bool] = None,
        *,
        show_preview: bool = True,
    ) -> str:
        """渲染评论并推送（或 dry-run）。返回人类可读的结果摘要。

        默认行为：``dry_run=True``（安全默认）。只有调用方显式传 ``dry_run=False``
        （CLI 上对应 ``--apply``）才会真实推送。
        """
        if dry_run is None:
            dry_run = True

        comment_md = render_lark_comment(report)

        if dry_run:
            if show_preview:
                logger.info("[dry-run] 渲染好的飞书评论 (%d chars):", len(comment_md))
                print("\n" + "─" * 70)
                print(comment_md)
                print("─" * 70 + "\n")
            carrier = self.cfg.meego_report_carrier_id or "(未配置 MEEGO_REPORT_CARRIER_ID)"
            return (
                f"[dry-run] 评论未推送（carrier={carrier}）。"
                f"如需真实发表，加 --apply 参数；预计 @ {len(report.mentions)} 位负责人。"
            )

        # 真实推送
        if not self.cfg.meego_report_carrier_id:
            raise RuntimeError(
                "MEEGO_REPORT_CARRIER_ID 未配置，无法推送。请在 .env 设置承载工作项 ID。"
            )
        if not self.cfg.meego_report_carrier_type_key:
            raise RuntimeError(
                "MEEGO_REPORT_CARRIER_TYPE_KEY 未配置（一般是 684a81a489c47be26942c57e）。"
            )

        try:
            self.meego.initialize()
            result = self.meego.add_comment(
                project_key=self.cfg.meego_project_key,
                work_item_id=self.cfg.meego_report_carrier_id,
                work_item_type_key=self.cfg.meego_report_carrier_type_key,
                content_markdown=comment_md,
            )
        except MeegoMCPError as e:
            self._audit(report, status="error", detail=str(e))
            raise

        self._audit(report, status="ok", detail=str(result)[:200])
        return (
            f"✅ 评论已发表到 工作项 #{self.cfg.meego_report_carrier_id}；"
            f"@{len(report.mentions)} 位负责人。"
        )

    # ------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------

    def _audit(self, report: ReportData, *, status: str, detail: str = "") -> None:
        try:
            log_path: Path = self.cfg.ensure_data_dir() / "push_history.log"
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = (
                f"{ts} | status={status} | "
                f"carrier={self.cfg.meego_report_carrier_id or '(dry-run)'} | "
                f"done={report.done_count}/{report.total_count} | "
                f"risks={len(report.risks)} | mentions={len(report.mentions)} | "
                f"detail={detail}\n"
            )
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("写审计日志失败（不影响主流程）: %s", e)
