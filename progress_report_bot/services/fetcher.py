"""F1 数据采集：飞书 → ProjectSnapshot。

主流程:
    1) list_todo(action="todo") 全量翻页拿"当前待办"
    2) list_done_since(now - 7d) 拿本周已完成（按节点完成事件，去重后是本周有动静的工作项）
    3) 合并两侧 work_item_id，按 (project_key, type_key, id) 去重
    4) 对每个 work_item 调一次 get_node_detail 拿:
        - 所有节点的状态 / is_delayed / 排期
        - field_1946d0 (开发分支)
        - field_8f07fb (选择仓库)
        - assignees.owners (负责人)
    5) 落 ``data/snapshot.json``

Git enrich：对有开发分支的工作项调 GitHub/GitLab REST，填充 BranchActivity。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import Config
from ..models import (
    NodeSchedule,
    Person,
    ProjectSnapshot,
    WorkItem,
    WorkItemEnriched,
    WorkItemNode,
)
from .git_factory import make_git_client, resolve_repo, resolve_repos
from .meego_client import MeegoClient, MeegoMCPError

logger = logging.getLogger(__name__)


# ============================================================
# 字段 key 常量（来自真实探针，可在 .env 里覆写）
# ============================================================
FIELD_KEY_DEV_BRANCH = "field_1946d0"
FIELD_KEY_REPOS = "field_8f07fb"


# ============================================================
# Mapper: dict → dataclass
# ============================================================

def _ts_to_dt(ts_ms: Optional[int]) -> Optional[datetime]:
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000)
    except (TypeError, ValueError, OSError):
        return None


def _persons(raw_list: Optional[Iterable[Dict[str, Any]]]) -> List[Person]:
    if not raw_list:
        return []
    return [Person.from_meego(r) for r in raw_list if isinstance(r, dict)]


def _parse_node(raw_node: Dict[str, Any]) -> WorkItemNode:
    basic = raw_node.get("basic", {}) or {}
    assignees = raw_node.get("assignees", {}) or {}
    sched_raw = raw_node.get("schedule", {}) or {}

    schedule = NodeSchedule(
        estimate_start=_ts_to_dt(sched_raw.get("estimate_start_time")),
        estimate_finish=_ts_to_dt(sched_raw.get("estimate_finish_time")),
        actual_begin=_ts_to_dt(sched_raw.get("actual_begin_time")),
        actual_finish=_ts_to_dt(sched_raw.get("actual_finish_time")),
        is_delayed=bool(sched_raw.get("is_delayed")),
        points=sched_raw.get("points"),
        actual_work_time=sched_raw.get("actual_work_time"),
    )

    branch: Optional[str] = None
    repos: List[str] = []
    for fi in raw_node.get("form_items", []) or []:
        if not isinstance(fi, dict):
            continue
        fkey = fi.get("field_key")
        val = fi.get("value")
        label = fi.get("value_label")
        if fkey == FIELD_KEY_DEV_BRANCH and val:
            branch = str(val).strip() or None
        elif fkey == FIELD_KEY_REPOS:
            extracted = _extract_repo_names(val, label)
            if extracted:
                repos.extend(extracted)

    return WorkItemNode(
        node_key=str(basic.get("node_key") or basic.get("node_uuid") or ""),
        node_name=str(basic.get("name") or ""),
        status=str(basic.get("status") or ""),
        owners=_persons(assignees.get("owners")),
        schedule=schedule,
        branch=branch,
        repos=repos,
    )


def _extract_repo_names(value: Any, label: Any) -> List[str]:
    """tree-multi-select 字段的 value 通常是 JSON 字符串数组（如
    ``'["&3neskoa5d","&ey8ghlypk"]'``），label 多半为空。
    返回去重后的 short code / 名称列表。
    """
    import json as _json

    def _flat(x: Any) -> List[str]:
        if x is None:
            return []
        if isinstance(x, list):
            out: List[str] = []
            for v in x:
                out.extend(_flat(v))
            return out
        if isinstance(x, str):
            s = x.strip()
            if not s:
                return []
            # 飞书把 JSON 数组当字符串塞回来：'["&abc","&def"]'
            if s.startswith("[") and s.endswith("]"):
                try:
                    parsed = _json.loads(s)
                    if isinstance(parsed, list):
                        return _flat(parsed)
                except (ValueError, TypeError):
                    pass
            # 单字符串：可能是 "&abc"，也可能是 "repoA,repoB" 形式
            if "," in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s]
        return [str(x)]

    out: List[str] = []
    seen = set()
    for raw in _flat(label) + _flat(value):
        if raw and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _build_work_item(
    project_key: str,
    project_name: str,
    work_item_id: str,
    work_item_name: str,
    work_item_type_key: str,
    node_detail: Dict[str, Any],
) -> WorkItem:
    """从 get_node_detail 返回构建一个 WorkItem。"""
    raw_nodes = node_detail.get("list") or []
    nodes = [_parse_node(n) for n in raw_nodes if isinstance(n, dict)]

    # 当前节点：取第一个 status==doing 的节点；否则最后一个
    current = next((n for n in nodes if n.status == "doing"), None)
    if current is None and nodes:
        current = nodes[-1]

    # work item 级 owners：取当前节点的 owners
    owners = current.owners if current else []

    # work item 级 branch / repos / is_delayed
    branch: Optional[str] = None
    repos: List[str] = []
    is_delayed = False
    for n in nodes:
        if n.branch and not branch:
            branch = n.branch
        for r in n.repos:
            if r not in repos:
                repos.append(r)
        if n.status == "doing" and n.schedule.is_delayed:
            is_delayed = True

    return WorkItem(
        project_key=project_key,
        project_name=project_name,
        work_item_id=str(work_item_id),
        work_item_name=work_item_name,
        work_item_type_key=work_item_type_key,
        current_node_name=current.node_name if current else "",
        current_node_status=current.status if current else "",
        owners=owners,
        nodes=nodes,
        is_delayed=is_delayed,
        branch=branch,
        repos=repos,
    )


# ============================================================
# Fetcher
# ============================================================

class Fetcher:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.meego = MeegoClient(cfg.meego_mcp_url, cfg.meego_mcp_token)
        self.git = make_git_client(cfg)

    # ----- public -----

    SCOPE_MINE = "mine"
    SCOPE_PROJECT = "project"
    SCOPE_ALL = "all"

    def fetch(self, *, persist: bool = True, scope: str = "mine") -> ProjectSnapshot:
        """三种采集范围：

        - ``mine`` (默认)：list_todo 拉 token 持有者本人参与的工作项（快、范围窄）
        - ``project``    ：search_by_mql 按类型扫全空间（慢、覆盖全员）
        - ``all``        ：mine 与 project 合并去重
        """
        self.cfg.require_meego()
        self.meego.initialize()

        if scope == self.SCOPE_PROJECT:
            return self._fetch_project_scope(persist=persist)
        if scope == self.SCOPE_ALL:
            snap_mine = self._fetch_user_scope(persist=False)
            snap_proj = self._fetch_project_scope(persist=False)
            return self._merge_snapshots(snap_mine, snap_proj, persist=persist)
        if scope != self.SCOPE_MINE:
            raise ValueError(f"unknown scope: {scope!r} (expected mine/project/all)")
        return self._fetch_user_scope(persist=persist)

    # ----- scope=mine: 原行为（list_todo） -----

    def _fetch_user_scope(self, *, persist: bool) -> ProjectSnapshot:
        project_key = self.cfg.meego_project_key
        window_days = self.cfg.report_window_days
        since = datetime.now() - timedelta(days=window_days)

        logger.info("[scope=mine] 拉取 todo（全量翻页）...")
        todo_raws = self.meego.list_todo_all_pages(action="todo", max_pages=5)
        logger.info("  → %d 条 todo", len(todo_raws))

        logger.info("[scope=mine] 拉取 done（since=%s）...", since.strftime("%Y-%m-%d %H:%M"))
        done_raws = self.meego.list_done_since(since=since, max_pages=10)
        logger.info("  → %d 条 done (本周节点完成事件)", len(done_raws))

        # 项目名（从首条数据取；若没有则用 project_key）
        project_name = (
            (todo_raws + done_raws)[0].get("project_name")
            if (todo_raws + done_raws)
            else project_key
        )

        # 按 work_item_id 去重，构建 (id, type_key, name) 索引
        index: Dict[str, Tuple[str, str]] = {}  # id -> (type_key, name)
        todo_ids: List[str] = []
        done_ids: List[str] = []
        for src, bucket in ((todo_raws, todo_ids), (done_raws, done_ids)):
            for it in src:
                wi = it.get("work_item_info", {}) or {}
                # 只关心当前 project_key 内的；list_todo 返回的也可能跨空间
                if it.get("project_key") and it.get("project_key") != project_key:
                    continue
                wid = str(wi.get("work_item_id") or "")
                tkey = str(wi.get("work_item_type_key") or "")
                name = str(wi.get("work_item_name") or "")
                if not wid or not tkey:
                    continue
                index.setdefault(wid, (tkey, name))
                if wid not in bucket:
                    bucket.append(wid)

        all_ids = list(index.keys())
        logger.info(
            "去重后共 %d 个工作项需要拉详情（todo=%d, done=%d）",
            len(all_ids),
            len(todo_ids),
            len(done_ids),
        )

        # 逐项 get_node_detail
        items_by_id: Dict[str, WorkItem] = {}
        failed: List[Tuple[str, str]] = []
        for i, wid in enumerate(all_ids, 1):
            tkey, name = index[wid]
            try:
                node_id = self._guess_current_node_key(wid, todo_raws, done_raws) or "state_3"
                detail = self.meego.get_node_detail(
                    project_key=project_key,
                    work_item_id=wid,
                    work_item_type_key=tkey,
                    node_id=node_id,
                )
                wi = _build_work_item(
                    project_key=project_key,
                    project_name=project_name,
                    work_item_id=wid,
                    work_item_name=name,
                    work_item_type_key=tkey,
                    node_detail=detail,
                )
                items_by_id[wid] = wi
                logger.info(
                    "  [%d/%d] #%s %s — node=%s/%s branch=%s delayed=%s",
                    i,
                    len(all_ids),
                    wid,
                    name[:30],
                    wi.current_node_name,
                    wi.current_node_status,
                    wi.branch,
                    wi.is_delayed,
                )
            except MeegoMCPError as e:
                failed.append((wid, str(e)))
                logger.warning("  [%d/%d] #%s 拉取失败: %s", i, len(all_ids), wid, e)

        if failed:
            logger.warning("共 %d 个工作项拉取失败（已跳过）", len(failed))

        todo_items = [items_by_id[w] for w in todo_ids if w in items_by_id]
        done_items = [items_by_id[w] for w in done_ids if w in items_by_id]

        enriched = self._enrich_github(
            [items_by_id[w] for w in all_ids if w in items_by_id],
            since=since,
        )

        snapshot = ProjectSnapshot(
            project_key=project_key,
            project_name=project_name,
            fetched_at=datetime.now(),
            window_days=window_days,
            todo_items=todo_items,
            done_items=done_items,
            enriched=enriched,
        )

        if persist:
            self._dump(snapshot)
        return snapshot

    # ----- scope=project: search_by_mql 全空间扫描 -----

    def _fetch_project_scope(self, *, persist: bool) -> ProjectSnapshot:
        project_key = self.cfg.meego_project_key
        window_days = self.cfg.report_window_days
        since = datetime.now() - timedelta(days=window_days)

        scan_types = self.cfg.scan_type_list
        if not scan_types:
            raise RuntimeError(
                "scope=project 需要 MEEGO_SCAN_TYPES (.env)，例如：MEEGO_SCAN_TYPES=执行需求"
            )

        space_label = self.cfg.meego_space_simple_name.strip()
        project_name = project_key
        if not space_label:
            try:
                d = self.meego.search_project_info(project_key=project_key)
                for p in (d.get("projects") or d.get("list") or []):
                    if p.get("project_key") == project_key:
                        space_label = str(p.get("simple_name") or project_key)
                        project_name = str(p.get("name") or project_key)
                        break
            except Exception as e:  # noqa: BLE001
                logger.warning("查 simple_name 失败，退回 project_key: %s", e)
            if not space_label:
                space_label = project_key

        logger.info(
            "[scope=project] MQL 扫描类型 %s @ space=%s",
            scan_types,
            space_label,
        )

        # 1) 用 MQL 拉每个类型的全部 (work_item_id, name, updated_at, type_key)
        rows_by_id: Dict[str, Tuple[str, str, Optional[datetime]]] = {}
        for type_name in scan_types:
            type_key = self._resolve_type_key(project_key, type_name)
            try:
                raw_rows = self.meego.search_workitems_by_mql_all_pages(
                    project_key=project_key,
                    space_label=space_label,
                    type_name=type_name,
                    fields=["work_item_id", "name", "updated_at"],
                    max_pages=20,
                )
            except MeegoMCPError as e:
                logger.warning("MQL 扫描类型 %r 失败: %s", type_name, e)
                continue
            logger.info("  类型 %r → %d 项", type_name, len(raw_rows))
            for r in raw_rows:
                wid = str(r.get("work_item_id") or "")
                if not wid or wid in rows_by_id:
                    continue
                name = str(r.get("name") or "")
                ts_str = str(r.get("updated_at") or "")
                updated = self._parse_loose_datetime(ts_str)
                rows_by_id[wid] = (type_key, name, updated)

        # 2) 本地按 updated_at 过滤到时间窗口内
        in_window = [
            (wid, t, n) for wid, (t, n, u) in rows_by_id.items()
            if u is None or u >= since
        ]
        logger.info(
            "[scope=project] MQL 共 %d 项，时间窗口内 (>= %s) %d 项",
            len(rows_by_id),
            since.strftime("%Y-%m-%d"),
            len(in_window),
        )

        # 3) 对窗口内的工作项调 get_node_detail 取详情
        items_by_id: Dict[str, WorkItem] = {}
        failed: List[Tuple[str, str]] = []
        for i, (wid, tkey, name) in enumerate(in_window, 1):
            try:
                detail = self.meego.get_node_detail(
                    project_key=project_key,
                    work_item_id=wid,
                    work_item_type_key=tkey,
                    node_id="state_3",
                )
                wi = _build_work_item(
                    project_key=project_key,
                    project_name=project_name,
                    work_item_id=wid,
                    work_item_name=name,
                    work_item_type_key=tkey,
                    node_detail=detail,
                )
                items_by_id[wid] = wi
                logger.info(
                    "  [%d/%d] #%s %s — node=%s/%s branch=%s",
                    i, len(in_window), wid, name[:30],
                    wi.current_node_name, wi.current_node_status, wi.branch,
                )
            except MeegoMCPError as e:
                failed.append((wid, str(e)))
                logger.warning("  [%d/%d] #%s 拉取失败: %s", i, len(in_window), wid, e)
        if failed:
            logger.warning("共 %d 项 get_node_detail 失败（已跳过）", len(failed))

        # 4) todo / done 分桶：未完成 → todo；已完成 → done
        todo_items: List[WorkItem] = []
        done_items: List[WorkItem] = []
        for wi in items_by_id.values():
            if wi.current_node_status == "finished" or "完成" in (wi.current_node_status or ""):
                done_items.append(wi)
            else:
                todo_items.append(wi)

        enriched = self._enrich_github(list(items_by_id.values()), since=since)
        snapshot = ProjectSnapshot(
            project_key=project_key,
            project_name=project_name,
            fetched_at=datetime.now(),
            window_days=window_days,
            todo_items=todo_items,
            done_items=done_items,
            enriched=enriched,
        )
        if persist:
            self._dump(snapshot)
        return snapshot

    def _merge_snapshots(
        self,
        a: ProjectSnapshot,
        b: ProjectSnapshot,
        *,
        persist: bool,
    ) -> ProjectSnapshot:
        """合并两个 snapshot，按 work_item_id 去重；a 中已存在的优先（含完整 owner 信息）。"""
        seen: set = {w.work_item_id for w in a.todo_items + a.done_items}

        todo = list(a.todo_items)
        done = list(a.done_items)
        for w in b.todo_items:
            if w.work_item_id not in seen:
                todo.append(w)
                seen.add(w.work_item_id)
        for w in b.done_items:
            if w.work_item_id not in seen:
                done.append(w)
                seen.add(w.work_item_id)

        # enriched 也合并去重
        enriched_seen: set = {e.work_item.work_item_id for e in a.enriched}
        enriched = list(a.enriched)
        for e in b.enriched:
            if e.work_item.work_item_id not in enriched_seen:
                enriched.append(e)
                enriched_seen.add(e.work_item.work_item_id)

        snap = ProjectSnapshot(
            project_key=a.project_key,
            project_name=a.project_name or b.project_name,
            fetched_at=datetime.now(),
            window_days=a.window_days,
            todo_items=todo,
            done_items=done,
            enriched=enriched,
        )
        if persist:
            self._dump(snap)
        return snap

    def _resolve_type_key(self, project_key: str, type_name: str) -> str:
        """把中文类型名解析为 type_key（如 ``执行需求`` → ``684a81a489c47be26942c57e``）。
        若 type_name 本身就是 type_key（无中文）直接返回。
        """
        if not any("\u4e00" <= ch <= "\u9fff" for ch in type_name):
            return type_name
        try:
            types = self.meego.list_workitem_types(project_key)
        except Exception:  # noqa: BLE001
            return type_name
        for t in types:
            if t.get("name") == type_name:
                return str(t.get("type_key") or type_name)
        return type_name

    @staticmethod
    def _parse_loose_datetime(s: str) -> Optional[datetime]:
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    # ----- helpers -----

    @staticmethod
    def _guess_current_node_key(
        wid: str,
        todo_raws: List[Dict[str, Any]],
        done_raws: List[Dict[str, Any]],
    ) -> Optional[str]:
        for src in (todo_raws, done_raws):
            for it in src:
                if str((it.get("work_item_info") or {}).get("work_item_id")) == wid:
                    node = it.get("node_info") or {}
                    nk = node.get("node_state_key") or node.get("node_key")
                    if nk:
                        return str(nk)
        return None

    def _resolve_repo(self, work_item: WorkItem) -> Optional[str]:
        return resolve_repo(self.cfg, work_item) or None

    def _enrich_github(
        self, items: List[WorkItem], since: datetime
    ) -> List[WorkItemEnriched]:
        enriched: List[WorkItemEnriched] = []
        if not self.git.enabled:
            logger.info(
                "Git provider 已禁用（provider=%s），将进入纯飞书模式",
                self.cfg.git_provider,
            )
            return [WorkItemEnriched(work_item=w) for w in items]

        # 智能降级：若所有工作项都没填「开发分支」字段，则跳过 git enrich
        has_any_branch = any(bool(w.branch) for w in items)
        if not has_any_branch:
            logger.info(
                "本项目空间无任何工作项填了「开发分支」字段，自动进入纯飞书模式（跳过 git enrich）",
            )
            return [WorkItemEnriched(work_item=w) for w in items]

        targets = self.cfg.merge_target_branch_list
        for w in items:
            wi_enriched = WorkItemEnriched(work_item=w)
            if not w.branch:
                enriched.append(wi_enriched)
                continue
            repos = resolve_repos(self.cfg, w)
            if not repos:
                logger.debug(
                    "#%s 有分支 %s 但无 repo 配置（repos=%s），跳过",
                    w.work_item_id,
                    w.branch,
                    w.repos,
                )
                enriched.append(wi_enriched)
                continue

            wi_enriched.git = self._enrich_one_workitem(
                w, repos=repos, since=since, targets=targets
            )
            enriched.append(wi_enriched)
        return enriched

    def _enrich_one_workitem(
        self,
        w: WorkItem,
        repos: List[str],
        since: datetime,
        targets: List[str],
    ):
        """对一个工作项扫多个仓库，聚合到一个 BranchActivity 返回。"""
        from pathlib import Path as _P

        from ..models import BranchActivity

        per_repo: List[BranchActivity] = []
        for repo in repos:
            try:
                act = self.git.get_branch_activity(
                    repo=repo,
                    branch=w.branch,
                    since=since,
                    target_branches=targets,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "  Git enrich 失败 #%s repo=%s branch=%s: %s",
                    w.work_item_id,
                    repo,
                    w.branch,
                    e,
                )
                continue
            per_repo.append(act)
            logger.info(
                "  Git enrich #%s [%s]@%s → exists=%s commits=%d prs=%d",
                w.work_item_id,
                _P(repo).name or repo,
                w.branch,
                act.exists,
                act.commit_count,
                len(act.pull_requests),
            )

        if not per_repo:
            return None

        # 聚合：repo 字段写 "name1, name2"；commits/PR 全合并；exists = any
        repo_labels = [_P(a.repo).name or a.repo for a in per_repo]
        agg = BranchActivity(
            repo=", ".join(repo_labels),
            branch=w.branch or "",
            exists=any(a.exists for a in per_repo),
        )
        for a in per_repo:
            agg.commits.extend(a.commits)
            agg.pull_requests.extend(a.pull_requests)
        return agg

    def _dump(self, snapshot: ProjectSnapshot) -> Path:
        out_dir = self.cfg.ensure_data_dir()
        out_path = out_dir / "snapshot.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(
                dataclasses.asdict(snapshot),
                f,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            )
        logger.info("snapshot 已落盘: %s", out_path)
        return out_path


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")
