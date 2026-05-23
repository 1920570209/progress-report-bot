"""progress-report-bot 命令行入口。

当前可用命令：
- ``ping``     验证 MCP token 有效性 + 打印服务器信息
- ``projects`` 列出 token 能看到的所有飞书项目空间
- ``todos``    拉一份当前用户的本周待办（默认 action=this_week）

后续会扩展：
- ``fetch``    飞书 + GitHub/GitLab 数据采集 → data/snapshot.json
- ``report``   基于 snapshot 生成 data/report.md
- ``push``     评论 + @负责人推送
- ``run-all``  端到端
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import Config
from .services.analyzer import Analyzer
from .services.diff_analyzer import DiffAnalyzer, format_diff_terminal
from .services.fetcher import Fetcher
from .services.meego_client import MeegoClient, MeegoMCPError
from .services.pusher import Pusher
from .services.renderer import render_diff_markdown, render_markdown
from .models import ReportData
from .services.sync import SyncService, format_sync_report
from .snapshot_io import load_snapshot, snapshot_path


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ------------------------------------------------------------
# Commands
# ------------------------------------------------------------

def cmd_ping(cfg: Config, args: argparse.Namespace) -> int:
    cfg.require_meego()
    client = MeegoClient(cfg.meego_mcp_url, cfg.meego_mcp_token)
    info = client.initialize()
    print("[ok] MCP server reachable")
    _print_json({"server": info, "url": cfg.meego_mcp_url})
    if args.list_tools:
        tools = client.list_tools()
        print(f"\n[tools] {len(tools)} 个可用工具:")
        for t in tools:
            print(f"  - {t.get('name')}")
    return 0


def cmd_projects(cfg: Config, args: argparse.Namespace) -> int:
    cfg.require_meego()
    client = MeegoClient(cfg.meego_mcp_url, cfg.meego_mcp_token)
    d = client.search_project_info()
    projects = d.get("projects") or d.get("list") or []
    print(f"[ok] 共 {len(projects)} 个有权限的空间：\n")
    for p in projects:
        marker = " ★" if p.get("project_key") == cfg.meego_project_key else ""
        print(
            f"  - {p.get('name')!r:20}  key={p.get('project_key')}  "
            f"simple_name={p.get('simple_name')}{marker}"
        )
    print("\n(★ = 当前 .env 默认 MEEGO_PROJECT_KEY)")
    return 0


def cmd_todos(cfg: Config, args: argparse.Namespace) -> int:
    cfg.require_meego()
    client = MeegoClient(cfg.meego_mcp_url, cfg.meego_mcp_token)
    items = client.list_todo_all_pages(action=args.action, max_pages=args.max_pages)
    print(f"[ok] 拉到 {len(items)} 条 action={args.action} 工作项：\n")
    for it in items:
        wi = it.get("work_item_info", {}) or {}
        node = it.get("node_info", {}) or {}
        sched = it.get("schedule", {}) or {}
        print(
            f"  #{wi.get('work_item_id')}  [{node.get('node_name')}]  "
            f"{wi.get('work_item_name')}  "
            f"({sched.get('start_time') or '-'} → {sched.get('end_time') or '-'})"
        )
    return 0


def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    fetcher = Fetcher(cfg)
    scope = getattr(args, "scope", "mine") or "mine"
    snap = fetcher.fetch(persist=True, scope=scope)
    print("\n" + "=" * 70)
    print(f"[ok] snapshot 已生成 → {cfg.data_dir / 'snapshot.json'}")
    print("=" * 70)
    print(f"  project   : {snap.project_name} ({snap.project_key})")
    print(f"  window    : 最近 {snap.window_days} 天")
    print(f"  todo      : {len(snap.todo_items)} 个未完成工作项")
    print(f"  done      : {len(snap.done_items)} 个本周有节点完成")
    delayed = [w for w in snap.todo_items if w.is_delayed]
    print(f"  delayed   : {len(delayed)} 个延期项")
    branched = [w for w in snap.todo_items if w.branch]
    print(f"  branched  : {len(branched)} 个 todo 有开发分支字段")
    print()
    if delayed:
        print("  ⚠ 延期项 (Top 5):")
        for w in delayed[:5]:
            owner = w.primary_owner.name if w.primary_owner else "?"
            print(
                f"    - #{w.work_item_id}  [{w.current_node_name}]  "
                f"{w.work_item_name[:40]}  by {owner}  branch={w.branch or '-'}"
            )
    return 0


def _load_snapshot(cfg: Config, use_cache: bool, scope: str = "mine"):
    """use_cache=True 时优先读 data/snapshot.json，不存在则在线拉取。"""
    cache = snapshot_path(cfg.data_dir)
    if use_cache and cache.exists():
        print(f"[cache] 使用已有快照 → {cache}")
        return load_snapshot(cache)
    fetcher = Fetcher(cfg)
    return fetcher.fetch(persist=True, scope=scope)


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    """F1 + F2: fetch → analyze (+ diff) → render → data/report.md。"""
    snap = _load_snapshot(cfg, args.use_cache, scope=getattr(args, "scope", "mine"))
    analyzer = Analyzer(cfg)
    report = analyzer.analyze(snap)

    md = render_markdown(report)
    out_path: Path = cfg.ensure_data_dir() / "report.md"
    out_path.write_text(md, encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"[ok] report 已生成 → {out_path}")
    print("=" * 70)
    print(f"  summary    : {report.summary_oneline}")
    print(
        f"  done/total : {report.done_count}/{report.total_count} "
        f"({int(round(report.completion_rate * 100))}%)"
    )
    print(f"  delayed    : {len(report.delayed_items)}")
    print(f"  risks      : {len(report.risks)}")
    if report.risks:
        for r in report.risks:
            icon = {"critical": "🔴", "warning": "🟡"}.get(r.severity, "🔵")
            print(
                f"    {icon} {r.work_item.work_item_name[:40]} — {r.reason}"
            )
    if args.show:
        print("\n" + "-" * 70 + "\n" + md)
    return 0


def _write_local_artifacts(cfg: Config, report: ReportData) -> tuple:
    """落盘 report.md + diff.md（如果有 diff），返回 (report_path, diff_path|None)。"""
    out_dir = cfg.ensure_data_dir()
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    diff_path = None
    if report.diff is not None:
        diff_path = out_dir / "diff.md"
        diff_path.write_text(render_diff_markdown(report.diff), encoding="utf-8")
    return md_path, diff_path


def cmd_push(cfg: Config, args: argparse.Namespace) -> int:
    """F1 + F2 + F3: 生成本地 md + （可选）推送评论到飞书。

    **默认行为 = 只生成本地文档**（``data/report.md`` + ``data/diff.md``），
    评论是 opt-in：必须显式 ``--apply`` 才会真实推送到飞书工作项。
    """
    snap = _load_snapshot(cfg, args.use_cache, scope=getattr(args, "scope", "mine"))
    report = Analyzer(cfg).analyze(snap)

    md_path, diff_path = _write_local_artifacts(cfg, report)

    dry_run = not bool(args.apply)
    pusher = Pusher(cfg)
    summary = pusher.push(report, dry_run=dry_run, show_preview=dry_run)

    print("\n" + "=" * 70)
    print(f"[ok] 本地文档已生成：")
    print(f"     - report : {md_path}")
    if diff_path:
        print(f"     - diff   : {diff_path}")
    print("=" * 70)
    print(f"[push] {summary}")
    if dry_run and cfg.meego_report_carrier_id:
        print(
            "\n提示：以上仅生成本地 md。如需真实发表评论到飞书工作项，运行:"
            "\n  python -m progress_report_bot push --apply"
        )
    return 0


def cmd_run_all(cfg: Config, args: argparse.Namespace) -> int:
    """端到端：fetch → analyze → render → push（按 .env 决定 dry-run）。"""
    return cmd_push(cfg, args)


def cmd_repos(cfg: Config, args: argparse.Namespace) -> int:
    """诊断：本地容器目录子仓库 + 飞书出现过的 short code + 当前映射缺口。"""
    import subprocess
    from pathlib import Path as _P

    print("\n" + "=" * 70)
    print("[1] GIT_PROVIDER =", cfg.git_provider)
    print("    LOCAL_GIT_REPO_ROOT =", cfg.local_git_repo_root or "(空)")
    print("    LOCAL_GIT_REPO_PATH =", cfg.local_git_repo_path)

    print("\n[2] 容器目录子仓库（候选 mapping 目标）：")
    sub_repos: list = []
    if cfg.local_git_repo_root:
        root = _P(cfg.local_git_repo_root)
        if root.exists():
            for d in sorted(root.iterdir()):
                if not d.is_dir() or not (d / ".git").exists():
                    continue
                try:
                    remote = subprocess.check_output(
                        ["git", "-C", str(d), "remote", "get-url", "origin"],
                        encoding="utf-8",
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    ).strip()
                except Exception:
                    remote = ""
                sub_repos.append((d.name, remote))
                print(f"  - {d.name:45}  {remote}")
        else:
            print(f"  ⚠ 路径不存在: {root}")
    else:
        print("  (未配置 LOCAL_GIT_REPO_ROOT)")

    print("\n[3] 当前 REPO_ID_MAP 解析结果：")
    mp = cfg.repo_id_map_dict
    if not mp:
        print("  (空)")
    for k, v in mp.items():
        print(f"  {k}  →  {v}")

    print("\n[4] 飞书快照中出现过的 short code（待映射）：")
    snap_path = cfg.data_dir / "snapshot.json"
    if not snap_path.exists():
        print("  (没有 data/snapshot.json，先跑一次 fetch)")
        return 0
    snap = load_snapshot(snap_path)
    seen: dict = {}
    for e in snap.enriched:
        for rid in e.work_item.repos:
            seen.setdefault(rid, []).append(e.work_item.work_item_id)
    if not seen:
        print("  (无工作项填了「选择仓库」字段)")
    else:
        for rid, ids in seen.items():
            mapped = mp.get(rid, "❌ 未映射")
            print(f"  {rid:18} → {mapped:40}  使用方: {', '.join(ids[:3])}")

    print("\n[5] 建议 .env 模板（已根据子目录名占位猜测，请按实际改）：")
    snippet = []
    for rid in seen:
        if rid in mp:
            snippet.append(f"{rid}={mp[rid]}")
            continue
        guess = sub_repos[0][0] if sub_repos else "<请填子目录名>"
        snippet.append(f"{rid}={guess}")
    if snippet:
        print("  REPO_ID_MAP=" + ",".join(snippet))
    print("=" * 70 + "\n")
    return 0


def cmd_fetch_repos(cfg: Config, args: argparse.Namespace) -> int:
    """对 LOCAL_GIT_REPO_ROOT 下所有子 git 仓库批量 git fetch，确保远端分支信息最新。"""
    import subprocess
    from pathlib import Path as _P

    root = _P(cfg.local_git_repo_root or cfg.local_git_repo_path)
    if not root.exists():
        print(f"[error] 路径不存在: {root}", file=sys.stderr)
        return 1

    targets = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / ".git").exists():
            targets.append(d)
    if (root / ".git").exists() and root not in targets:
        targets.append(root)

    if not targets:
        print(f"[warn] {root} 下没找到任何 git 仓库")
        return 0

    print(f"\n开始 git fetch {len(targets)} 个仓库（root={root}）...\n")
    ok = 0
    fail = 0
    for d in targets:
        try:
            subprocess.run(
                ["git", "-C", str(d), "fetch", "--quiet", "--prune"],
                check=True,
                timeout=60,
            )
            print(f"  ✓ {d.name}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d.name}: {e}")
            fail += 1
    print(f"\n[done] ok={ok}  fail={fail}")
    return 0 if fail == 0 else 1


def cmd_diff(cfg: Config, args: argparse.Namespace) -> int:
    """F6: 飞书项目状态 ↔ Git 实际进度 对账。"""
    snap = _load_snapshot(cfg, args.use_cache, scope=getattr(args, "scope", "mine"))
    diff = DiffAnalyzer(cfg).analyze(snap)

    md = render_diff_markdown(diff)
    out_path: Path = cfg.ensure_data_dir() / "diff.md"
    out_path.write_text(md, encoding="utf-8")

    print("\n" + "=" * 70)
    print(format_diff_terminal(diff))
    print("=" * 70)
    print(f"[ok] diff 已生成 → {out_path}")
    return 0


# ------------------------------------------------------------
# init: 交互式向导（首跑必经）
# ------------------------------------------------------------

def _ask(prompt: str, default: str = "", required: bool = False) -> str:
    """统一的交互问答；支持 tty 与管道喂值，EOF 时回退到 default。

    管道模式下若 required 仍为空，不死循环——直接返回空字符串，让上层校验。
    """
    suffix = f" [{default}]" if default else ""
    tag = " (必填)" if required else ""
    interactive = sys.stdin.isatty()
    while True:
        try:
            val = input(f"  {prompt}{tag}{suffix}: ").strip()
        except EOFError:
            val = ""
        if not val:
            val = default
        if val or not required:
            return val
        if not interactive:
            return ""
        print("    ! 必填，不能为空。")


def _detect_git(start: Path) -> tuple:
    """探测 start 周围的 git 形态，返回 (provider, [env_lines], summary_str)。

    优先级（重要：容器探测优先于 start 自身是 git）：
    1. start 下有 ≥2 个 git 子目录 → local + LOCAL_GIT_REPO_ROOT（容器模式）
    2. start 本身是 git → local + LOCAL_GIT_REPO_PATH
    3. start 下恰好 1 个 git 子目录 → local + LOCAL_GIT_REPO_PATH=该子目录
    4. 都没有 → none（纯飞书模式）

    顺序原因：很多团队的 monorepo 容器目录自身也被 git 管理（aggregate repo），
    若按"自身 git 优先"会把容器误判为单仓库，导致后续 monorepo 工作项扫不到。
    """
    start = start.resolve()
    sub_repos: list = []
    try:
        for child in start.iterdir():
            if child.is_dir() and (child / ".git").exists():
                sub_repos.append(child)
    except OSError:
        pass

    if len(sub_repos) >= 2:
        return (
            "local",
            [
                "GIT_PROVIDER=local",
                f"LOCAL_GIT_REPO_ROOT={start}",
                "LOCAL_GIT_REMOTE_PREFIX=origin/",
                "# REPO_ID_MAP=&xxxxx=sub_dir_name  # 飞书「选择仓库」字段用到时再配，可先跑 `repos` 命令拿建议",
            ],
            f"检测到 git 容器 → 启用 local 容器模式（root={start.name}，含 {len(sub_repos)} 个子仓库）",
        )

    if (start / ".git").exists():
        return (
            "local",
            [
                "GIT_PROVIDER=local",
                f"LOCAL_GIT_REPO_PATH={start}",
                "LOCAL_GIT_REMOTE_PREFIX=origin/",
            ],
            f"检测到 git 仓库 → 启用 local 模式（repo={start.name}）",
        )

    if len(sub_repos) == 1:
        sub = sub_repos[0]
        return (
            "local",
            [
                "GIT_PROVIDER=local",
                f"LOCAL_GIT_REPO_PATH={sub}",
                "LOCAL_GIT_REMOTE_PREFIX=origin/",
            ],
            f"检测到唯一 git 子仓库 → 启用 local 模式（repo={sub.name}）",
        )

    return (
        "none",
        ["GIT_PROVIDER=none"],
        "未检测到 git 仓库 → 使用纯飞书模式（任何时候改 .env 的 GIT_PROVIDER 即可切换）",
    )


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    """交互式初始化向导：问飞书必填参数 + 自动探测 git → 写 .env。"""
    env_path = Path.cwd() / ".env"
    if env_path.exists() and not args.force:
        print(f"[warn] .env 已存在：{env_path}")
        print("       如需覆盖请加 --force，或直接编辑该文件。")
        return 1

    print("\n" + "=" * 70)
    print("progress-report-bot · 初始化向导")
    print("=" * 70)
    print("将引导你完成最小可用配置。所有问题都在 .env 落盘，可随时手改。\n")

    print("[1/3] 飞书项目 MCP 必填项")
    token = _ask(
        "MEEGO_MCP_TOKEN（飞书项目 > 设置 > MCP 接入 > 复制 token）",
        default=cfg.meego_mcp_token,
        required=True,
    )
    project_key = _ask(
        "MEEGO_PROJECT_KEY（项目空间 key，跑一次 `projects` 可列出）",
        default=cfg.meego_project_key,
        required=True,
    )

    print("\n[2/3] 可选：评论承载工作项")
    print("       若想用 push --apply 把周报评论自动发到某个工作项，填它的 ID；")
    print("       留空则 push 只生成本地 md，不会动飞书。")
    carrier_id = _ask(
        "MEEGO_REPORT_CARRIER_ID（可选，留空跳过）",
        default=cfg.meego_report_carrier_id,
        required=False,
    )
    carrier_type_key = ""
    if carrier_id:
        carrier_type_key = _ask(
            "MEEGO_REPORT_CARRIER_TYPE_KEY（可选，飞书工作项类型 key）",
            default=cfg.meego_report_carrier_type_key or "684a81a489c47be26942c57e",
            required=False,
        )

    print("\n[3/3] 自动探测 git 仓库（当前目录: %s）" % Path.cwd())
    provider, git_lines, summary = _detect_git(Path.cwd())
    print(f"  → {summary}")

    lines = [
        "# Generated by `progress-report-bot init` —— 可随时手改",
        "",
        "# === 飞书项目 MCP ===",
        "MEEGO_MCP_URL=https://project.feishu.cn/mcp_server/v1",
        f"MEEGO_MCP_TOKEN={token}",
        f"MEEGO_PROJECT_KEY={project_key}",
        f"MEEGO_REPORT_CARRIER_ID={carrier_id}",
        f"MEEGO_REPORT_CARRIER_TYPE_KEY={carrier_type_key}",
        "MEEGO_FOCUS_WORK_ITEM_ID=",
        "",
        "# === Git provider（自动探测得出） ===",
    ]
    lines.extend(git_lines)
    lines.extend([
        "",
        "# === Sync / 安全护栏（按团队工作流改）===",
        "MERGE_TARGET_BRANCHES=test",
        "SYNC_SOURCE_NODE_NAME=功能开发",
        "SYNC_TARGET_NODE_NAMES=功能测试,提测,测试中",
        "SYNC_BRANCH_WHITELIST=",
        "",
        "# === 报告窗口 ===",
        "REPORT_WINDOW_DAYS=7",
        "",
    ])

    env_path.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"[ok] .env 已生成：{env_path}")
    print("=" * 70)
    print("\n下一步：")
    print("  1. 验证连通：  python -m progress_report_bot ping")
    if provider == "local":
        print("  2. 看一下数据：python -m progress_report_bot run-all   # 仅生成本地 md")
        print("  3. 多仓库自检：python -m progress_report_bot repos      # 看 short code 缺哪些映射")
    else:
        print("  2. 看一下数据：python -m progress_report_bot run-all   # 仅生成本地 md（纯飞书模式）")
    if carrier_id:
        print("  ★ 真发评论：  python -m progress_report_bot push --apply")
    else:
        print("  ★ 想自动发评论到工作项？把工作项 ID 填进 .env 的 MEEGO_REPORT_CARRIER_ID")
    print()
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    """F5: Git MR/PR 已合并到测试分支 → 飞书节点自动流转（默认 dry-run）。"""
    svc = SyncService(cfg)
    apply = bool(args.apply)
    result = svc.run(apply=apply)
    print("\n" + "=" * 70)
    print(format_sync_report(result))
    print("=" * 70)
    if not apply and result.candidates:
        print("\n提示: 以上仅为预览。确认无误后执行:")
        print("  python -m progress_report_bot sync --apply")
    return 0


# ------------------------------------------------------------
# Argparse wiring
# ------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="progress-report-bot",
        description="飞书项目进度自动周报机器人",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="开启 DEBUG 日志")

    sub = p.add_subparsers(dest="command", required=True)

    pp_ping = sub.add_parser("ping", help="验证 MCP token + 列出服务器信息")
    pp_ping.add_argument("--list-tools", action="store_true", help="同时列出所有工具")
    pp_ping.set_defaults(func=cmd_ping)

    pp_proj = sub.add_parser("projects", help="列出可访问的飞书项目空间")
    pp_proj.set_defaults(func=cmd_projects)

    pp_todo = sub.add_parser("todos", help="拉当前用户的待办/已办")
    pp_todo.add_argument(
        "--action",
        default="this_week",
        choices=["todo", "done", "overdue", "this_week"],
        help="查询类型，默认 this_week",
    )
    pp_todo.add_argument("--max-pages", type=int, default=3)
    pp_todo.set_defaults(func=cmd_todos)

    def _add_scope(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--scope",
            choices=["mine", "project", "all"],
            default="mine",
            help=(
                "采集范围：mine=token 持有者本人（默认，飞书 list_todo）；"
                "project=全空间扫描（飞书 search_by_mql，按 MEEGO_SCAN_TYPES 类型，"
                "需要在 .env 配 MEEGO_SCAN_TYPES）；all=两者合并去重"
            ),
        )

    pp_fetch = sub.add_parser(
        "fetch",
        help="F1: 拉飞书空间数据 → data/snapshot.json (后续 report/push 的输入)",
    )
    _add_scope(pp_fetch)
    pp_fetch.set_defaults(func=cmd_fetch)

    pp_report = sub.add_parser(
        "report",
        help="F1+F2: 拉数据 + 分析 + 渲染 → data/report.md",
    )
    pp_report.add_argument(
        "--show", action="store_true", help="同时把生成的 markdown 打印到终端"
    )
    pp_report.add_argument(
        "--use-cache",
        action="store_true",
        help="使用 data/snapshot.json（存在则跳过在线拉取，演示更快）",
    )
    _add_scope(pp_report)
    pp_report.set_defaults(func=cmd_report)

    pp_push = sub.add_parser(
        "push",
        help="F1+F2+F3: 拉数据 + 分析 + 推送到飞书工作项评论 (+ @负责人)",
    )
    g = pp_push.add_mutually_exclusive_group()
    g.add_argument(
        "--dry-run",
        action="store_true",
        help="只渲染评论到终端，不真推送（即便 carrier 已配置）",
    )
    g.add_argument(
        "--apply",
        action="store_true",
        help="即便 carrier 配置缺失也强制尝试推送（会报错，调试用）",
    )
    pp_push.add_argument(
        "--use-cache",
        action="store_true",
        help="使用 data/snapshot.json（存在则跳过在线拉取）",
    )
    _add_scope(pp_push)
    pp_push.set_defaults(func=cmd_push)

    pp_runall = sub.add_parser(
        "run-all",
        help="端到端：fetch → analyze → render → push (= push 的别名)",
    )
    g2 = pp_runall.add_mutually_exclusive_group()
    g2.add_argument("--dry-run", action="store_true")
    g2.add_argument("--apply", action="store_true")
    pp_runall.add_argument("--use-cache", action="store_true")
    _add_scope(pp_runall)
    pp_runall.set_defaults(func=cmd_run_all)

    pp_repos = sub.add_parser(
        "repos",
        help="列出本地容器子仓库 + 飞书出现过的 short code + 映射缺口诊断",
    )
    pp_repos.set_defaults(func=cmd_repos)

    pp_fr = sub.add_parser(
        "fetch-repos",
        help="对 LOCAL_GIT_REPO_ROOT 下所有 git 子仓库批量 git fetch",
    )
    pp_fr.set_defaults(func=cmd_fetch_repos)

    pp_diff = sub.add_parser(
        "diff",
        help="F6: 飞书项目状态 ↔ Git 实际进度 对账 → data/diff.md",
    )
    pp_diff.add_argument(
        "--use-cache",
        action="store_true",
        help="使用 data/snapshot.json（存在则跳过在线拉取，演示更快）",
    )
    _add_scope(pp_diff)
    pp_diff.set_defaults(func=cmd_diff)

    pp_sync = sub.add_parser(
        "sync",
        help="F5: PR 合并到测试分支后自动推进飞书节点（默认 dry-run）",
    )
    pp_sync.add_argument(
        "--apply",
        action="store_true",
        help="真实调用 transition_node（默认仅预览）",
    )
    pp_sync.set_defaults(func=cmd_sync)

    pp_init = sub.add_parser(
        "init",
        help="★ 首跑：交互式向导生成 .env（自动探测 git）",
    )
    pp_init.add_argument(
        "--force", action="store_true", help="即便 .env 已存在也覆盖"
    )
    pp_init.set_defaults(func=cmd_init)

    return p


# 这些命令缺关键配置时只是友好提示，不真去跑业务
_COMMANDS_NEED_NO_MEEGO = {"init"}


def _ensure_configured(cfg: Config, command: str) -> Optional[int]:
    """业务命令跑之前，确认 .env / 关键配置已就绪；缺则引导用户。

    返回 None 表示继续；返回 int 表示直接以该退出码退出。
    """
    if command in _COMMANDS_NEED_NO_MEEGO:
        return None

    env_file = Path.cwd() / ".env"
    if not env_file.exists() and not cfg.meego_mcp_token:
        print("=" * 70)
        print("还没有配置文件（当前目录找不到 .env）。")
        print("=" * 70)
        print("请先跑一次初始化向导：")
        print("  python -m progress_report_bot init")
        print()
        print("向导会问你：")
        print("  1) 飞书项目 MCP token + project key（必填）")
        print("  2) 评论承载工作项 ID（可选，留空只生成本地 md）")
        print("  3) 自动探测当前目录的 git，配好 local 模式（探测不到自动用纯飞书模式）")
        return 1

    if not cfg.meego_mcp_token or not cfg.meego_project_key:
        missing = []
        if not cfg.meego_mcp_token:
            missing.append("MEEGO_MCP_TOKEN")
        if not cfg.meego_project_key:
            missing.append("MEEGO_PROJECT_KEY")
        print(f"[error] 必需配置缺失: {', '.join(missing)}", file=sys.stderr)
        print(
            "        请编辑 .env 补全，或运行 `python -m progress_report_bot init --force` 重新初始化。",
            file=sys.stderr,
        )
        return 1
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = Config.from_env()

    pre = _ensure_configured(cfg, args.command)
    if pre is not None:
        return pre

    try:
        return args.func(cfg, args)
    except MeegoMCPError as me:
        print(f"[error] MCP 调用失败: {me}", file=sys.stderr)
        if me.code:
            print(f"        code={me.code}", file=sys.stderr)
        return 2
    except RuntimeError as re:
        print(f"[error] {re}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
