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
        carrier_id: Optional[str] = None,
        carrier_type_key: Optional[str] = None,
    ) -> str:
        """渲染评论并推送（或 dry-run）。返回人类可读的结果摘要。

        默认行为：``dry_run=True``（安全默认）。只有调用方显式传 ``dry_run=False``
        （CLI 上对应 ``--apply``）才会真实推送。
        """
        if dry_run is None:
            dry_run = True

        cid = (carrier_id or self.cfg.meego_report_carrier_id or "").strip()
        ctype = (carrier_type_key or self.cfg.meego_report_carrier_type_key or "").strip()
        comment_md = render_lark_comment(report)

        if dry_run:
            if show_preview:
                logger.info("[dry-run] 渲染好的飞书评论 (%d chars):", len(comment_md))
                print("\n" + "─" * 70)
                print(comment_md)
                print("─" * 70 + "\n")
            carrier = cid or "(未配置 MEEGO_REPORT_CARRIER_ID)"
            return (
                f"[dry-run] 评论未推送（carrier={carrier}）。"
                f"如需真实发表，加 --apply 参数；预计 @ {len(report.mentions)} 位负责人。"
            )

        # 真实推送
        if not cid:
            raise RuntimeError(
                "MEEGO_REPORT_CARRIER_ID 未配置，无法推送。"
                "请在 .env 设置承载工作项 ID，或使用 --select-carrier 交互选择。"
            )
        if not ctype:
            raise RuntimeError(
                "MEEGO_REPORT_CARRIER_TYPE_KEY 未配置。"
                "请重新 init 选择承载工作项，或使用 --select-carrier。"
            )

        try:
            self.meego.initialize()
            result = self.meego.add_comment(
                project_key=self.cfg.meego_project_key,
                work_item_id=cid,
                work_item_type_key=ctype,
                content_markdown=comment_md,
            )
        except MeegoMCPError as e:
            self._audit(report, status="error", detail=str(e), carrier_id=cid)
            raise

        self._audit(report, status="ok", detail=str(result)[:200], carrier_id=cid)
        return (
            f"✅ 评论已发表到 工作项 #{cid}；"
            f"@{len(report.mentions)} 位负责人。"
        )

    # ------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------

    def _audit(
        self,
        report: ReportData,
        *,
        status: str,
        detail: str = "",
        carrier_id: Optional[str] = None,
    ) -> None:
        try:
            log_path: Path = self.cfg.ensure_data_dir() / "push_history.log"
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cid = carrier_id or self.cfg.meego_report_carrier_id or "(dry-run)"
            line = (
                f"{ts} | status={status} | "
                f"carrier={cid} | "
                f"done={report.done_count}/{report.total_count} | "
                f"risks={len(report.risks)} | mentions={len(report.mentions)} | "
                f"detail={detail}\n"
            )
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("写审计日志失败（不影响主流程）: %s", e)
