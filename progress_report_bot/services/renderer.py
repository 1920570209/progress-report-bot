"""F2/F3 渲染：ReportData → Markdown / 飞书富文本评论。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models import DiffReport, ReportData
from .diff_analyzer import KIND_LABELS, SEV_ICON


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _env_markdown() -> Environment:
    """Markdown 报告：用 trim_blocks/lstrip_blocks 让模板缩进不污染输出。"""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape([]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _env_lark() -> Environment:
    """飞书评论：纯文本，trim 关闭，靠模板自身控制换行（避免 list 项被吃换行）。"""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape([]),
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def _build_context(report: ReportData) -> Dict[str, Any]:
    return {
        "report": report,
        "pct": int(round(report.completion_rate * 100)),
        "has_risks": bool(report.risks),
        "has_delayed": bool(report.delayed_items),
        "has_git": bool(report.git_items),
        "has_owners": bool(report.owner_lines),
        "has_progress": report.progress is not None and (
            bool(report.progress.completed)
            or bool(report.progress.active)
            or report.progress.pending_count > 0
        ),
        "has_diff": report.diff is not None,
        "kind_labels": KIND_LABELS,
        "sev_icon": SEV_ICON,
        "generated_at_str": report.generated_at.strftime("%Y-%m-%d %H:%M"),
    }


def render_markdown(report: ReportData) -> str:
    """渲染本地 report.md（人类可读，演示彩排用）。"""
    tmpl = _env_markdown().get_template("report.md.j2")
    return tmpl.render(**_build_context(report))


def render_lark_comment(report: ReportData) -> str:
    """渲染要 POST 到 add_comment 的飞书 markdown 富文本。"""
    tmpl = _env_lark().get_template("lark_comment.md.j2")
    return tmpl.render(**_build_context(report))


def render_diff_markdown(diff: DiffReport) -> str:
    """渲染独立 diff.md（飞书 ↔ Git 对账详情）。"""
    tmpl = _env_markdown().get_template("diff.md.j2")
    return tmpl.render(
        diff=diff,
        kind_labels=KIND_LABELS,
        sev_icon=SEV_ICON,
        generated_at_str=diff.generated_at.strftime("%Y-%m-%d %H:%M"),
    )
