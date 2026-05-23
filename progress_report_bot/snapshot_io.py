"""snapshot.json 读写（与 fetcher._dump 格式对称）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    BranchActivity,
    GitCommit,
    NodeSchedule,
    Person,
    ProjectSnapshot,
    PullRequest,
    WorkItem,
    WorkItemEnriched,
    WorkItemNode,
)


def _parse_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except ValueError:
        return None


def _person(d: Dict[str, Any]) -> Person:
    return Person(
        user_key=str(d.get("user_key") or ""),
        name=str(d.get("name") or ""),
        email=str(d.get("email") or ""),
    )


def _schedule(d: Optional[Dict[str, Any]]) -> NodeSchedule:
    d = d or {}
    return NodeSchedule(
        estimate_start=_parse_dt(d.get("estimate_start")),
        estimate_finish=_parse_dt(d.get("estimate_finish")),
        actual_begin=_parse_dt(d.get("actual_begin")),
        actual_finish=_parse_dt(d.get("actual_finish")),
        is_delayed=bool(d.get("is_delayed")),
        points=d.get("points"),
        actual_work_time=d.get("actual_work_time"),
    )


def _node(d: Dict[str, Any]) -> WorkItemNode:
    return WorkItemNode(
        node_key=str(d.get("node_key") or ""),
        node_name=str(d.get("node_name") or ""),
        status=str(d.get("status") or ""),
        owners=[_person(p) for p in (d.get("owners") or [])],
        schedule=_schedule(d.get("schedule")),
        branch=d.get("branch"),
        repos=list(d.get("repos") or []),
    )


def _work_item(d: Dict[str, Any]) -> WorkItem:
    return WorkItem(
        project_key=str(d.get("project_key") or ""),
        project_name=str(d.get("project_name") or ""),
        work_item_id=str(d.get("work_item_id") or ""),
        work_item_name=str(d.get("work_item_name") or ""),
        work_item_type_key=str(d.get("work_item_type_key") or ""),
        current_node_name=str(d.get("current_node_name") or ""),
        current_node_status=str(d.get("current_node_status") or ""),
        owners=[_person(p) for p in (d.get("owners") or [])],
        nodes=[_node(n) for n in (d.get("nodes") or [])],
        is_delayed=bool(d.get("is_delayed")),
        branch=d.get("branch"),
        repos=list(d.get("repos") or []),
        url=str(d.get("url") or ""),
    )


def _branch_activity(d: Optional[Dict[str, Any]]) -> Optional[BranchActivity]:
    if not d:
        return None
    commits = [
        GitCommit(
            sha=str(c.get("sha") or ""),
            message=str(c.get("message") or ""),
            author=str(c.get("author") or ""),
            date=_parse_dt(c.get("date")) or datetime.now(),
            url=str(c.get("url") or ""),
        )
        for c in (d.get("commits") or [])
    ]
    prs = [
        PullRequest(
            number=int(p.get("number") or 0),
            title=str(p.get("title") or ""),
            state=str(p.get("state") or ""),
            author=str(p.get("author") or ""),
            head_branch=str(p.get("head_branch") or ""),
            base_branch=str(p.get("base_branch") or ""),
            url=str(p.get("url") or ""),
            merged=bool(p.get("merged")),
        )
        for p in (d.get("pull_requests") or [])
    ]
    return BranchActivity(
        repo=str(d.get("repo") or ""),
        branch=str(d.get("branch") or ""),
        commits=commits,
        pull_requests=prs,
        exists=bool(d.get("exists", True)),
    )


def load_snapshot(path: Path) -> ProjectSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    enriched: List[WorkItemEnriched] = []
    for e in data.get("enriched") or []:
        wi = _work_item(e.get("work_item") or {})
        enriched.append(WorkItemEnriched(work_item=wi, git=_branch_activity(e.get("git"))))
    return ProjectSnapshot(
        project_key=str(data.get("project_key") or ""),
        project_name=str(data.get("project_name") or ""),
        fetched_at=_parse_dt(data.get("fetched_at")) or datetime.now(),
        window_days=int(data.get("window_days") or 7),
        todo_items=[_work_item(w) for w in (data.get("todo_items") or [])],
        done_items=[_work_item(w) for w in (data.get("done_items") or [])],
        enriched=enriched,
    )


def snapshot_path(data_dir: Path) -> Path:
    return data_dir / "snapshot.json"
