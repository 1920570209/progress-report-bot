"""飞书项目 (Meego) MCP 客户端，stdlib-only。

协议：JSON-RPC 2.0 over HTTP (Streamable HTTP, MCP 2025-03-26)。
鉴权：HTTP Header `X-Mcp-Token`。

设计原则：
- 只暴露我们实际需要的工具，避免 51 个工具全包装。
- 透传 dict（不强制类型转换），由上层 fetcher 决定如何映射到 dataclass。
- 错误统一抛 ``MeegoMCPError``。
"""

from __future__ import annotations

import itertools
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)


def _parse_finish_time(s: str) -> Optional[datetime]:
    """Parse ``"YYYY-MM-DD HH:MM"`` (Feishu local-tz naive) into naive datetime."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class MeegoMCPError(RuntimeError):
    """飞书 MCP 调用失败的统一异常。"""

    def __init__(self, message: str, *, code: Optional[int] = None, raw: Any = None):
        super().__init__(message)
        self.code = code
        self.raw = raw


class MeegoClient:
    """飞书项目 MCP 客户端。

    用法::

        client = MeegoClient(url, token)
        client.initialize()
        items = client.list_todo(action="todo", page=1)

    线程不安全。每个请求线程建议使用独立实例。
    """

    DEFAULT_PROTOCOL_VERSION = "2025-03-26"
    CLIENT_INFO = {"name": "progress-report-bot", "version": "0.1.0"}

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 1,
        retry_backoff: float = 1.0,
    ) -> None:
        if not url:
            raise ValueError("MCP url is required")
        if not token:
            raise ValueError("MCP token is required")
        self.url = url
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

        self._id_iter = itertools.count(1)
        self._initialized = False
        self._server_info: Dict[str, Any] = {}

    # ----------------------------------------------------------
    # JSON-RPC 底层
    # ----------------------------------------------------------

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "X-Mcp-Token": self.token,
            },
        )
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as je:
                    raise MeegoMCPError(
                        f"MCP 返回非 JSON：{raw[:300]}", raw=raw
                    ) from je
            except urllib.error.HTTPError as he:
                body_text = ""
                try:
                    body_text = he.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_err = MeegoMCPError(
                    f"HTTP {he.code}: {he.reason}; body={body_text[:300]}",
                    code=he.code,
                    raw=body_text,
                )
            except urllib.error.URLError as ue:
                last_err = MeegoMCPError(f"网络错误: {ue}")
            except Exception as e:  # noqa: BLE001
                last_err = MeegoMCPError(f"未知错误: {e}")

            if attempt < self.max_retries:
                wait = self.retry_backoff * (attempt + 1)
                logger.warning(
                    "MCP 调用失败将重试 (attempt=%d/%d, wait=%.1fs): %s",
                    attempt + 1,
                    self.max_retries,
                    wait,
                    last_err,
                )
                time.sleep(wait)
        assert last_err is not None
        raise last_err

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": next(self._id_iter),
            "method": method,
            "params": params or {},
        }
        resp = self._post(body)
        if "error" in resp and resp["error"]:
            err = resp["error"]
            raise MeegoMCPError(
                f"MCP RPC error {err.get('code')}: {err.get('message')}",
                code=err.get("code"),
                raw=resp,
            )
        return resp.get("result", {})

    # ----------------------------------------------------------
    # 协议级 API
    # ----------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        if self._initialized:
            return self._server_info
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self.DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )
        self._server_info = result.get("serverInfo", {})
        self._initialized = True
        logger.info("MCP initialized: %s", self._server_info)
        return self._server_info

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._rpc("tools/list")
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> List[Any]:
        """调用一个工具，返回解析后的 content 列表。

        - text 内容若是合法 JSON，会被自动 json.loads；
        - 否则保持为字符串。
        若服务端返回业务错误（content 里包含 "id=xxx, code=xxx, message=..."），
        会被识别为字符串并照样返回，调用方负责辨识；如想严格化可启用 ``raise_on_text_error``。
        """
        if not self._initialized:
            self.initialize()

        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        contents = result.get("content", []) or []
        parsed: List[Any] = []
        for c in contents:
            if not isinstance(c, dict):
                parsed.append(c)
                continue
            if c.get("type") == "text":
                text = c.get("text", "")
                try:
                    parsed.append(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    parsed.append(text)
            else:
                parsed.append(c)
        return parsed

    @staticmethod
    def first_dict(contents: Iterable[Any]) -> Dict[str, Any]:
        """取出 call_tool 返回的第一个 dict（最常见的业务载荷）。"""
        for c in contents:
            if isinstance(c, dict):
                return c
        return {}

    @staticmethod
    def detect_error(contents: Iterable[Any]) -> Optional[str]:
        """检测服务端在 text content 里返回的业务错误（如 'id=1000050156, code=20006, ...'）。"""
        for c in contents:
            if isinstance(c, str) and "code=" in c and "message=" in c:
                return c
        return None

    # ----------------------------------------------------------
    # 业务封装（按需逐步增加）
    # ----------------------------------------------------------

    # --- 项目 / 用户 ---

    def search_project_info(
        self, project_key: str = "", page_num: int = 1
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"page_num": page_num}
        if project_key:
            args["project_key"] = project_key
        contents = self.call_tool("search_project_info", args)
        return self.first_dict(contents)

    def search_user_info(self, query: str) -> List[Dict[str, Any]]:
        contents = self.call_tool("search_user_info", {"query": query})
        d = self.first_dict(contents)
        for k in ("users", "list", "data"):
            if isinstance(d.get(k), list):
                return d[k]
        return []

    def list_workitem_types(self, project_key: str) -> List[Dict[str, Any]]:
        contents = self.call_tool(
            "list_workitem_types", {"project_key": project_key}
        )
        d = self.first_dict(contents)
        for k in ("work_item_types", "type_list", "list", "data"):
            if isinstance(d.get(k), list):
                return d[k]
        return []

    # --- 待办 / 进度 ---

    def list_todo(self, action: str = "todo", page: int = 1) -> Dict[str, Any]:
        """action: todo | done | overdue | this_week."""
        contents = self.call_tool(
            "list_todo", {"action": action, "page_num": page}
        )
        return self.first_dict(contents)

    def list_todo_all_pages(
        self, action: str = "todo", max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        """翻页拿全部，max_pages 兜底防失控。"""
        items: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            d = self.list_todo(action=action, page=page)
            batch = d.get("list") or []
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 50:  # 默认 page_size=50
                break
        return items

    def list_done_since(
        self,
        since: "datetime",
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """拉 action=done 翻页直到 finish_time < since 为止。

        返回的每条 item 里 ``finish_time.finish_time`` 是 ``"YYYY-MM-DD HH:MM"``
        字符串格式（飞书项目用户本地时区）；本方法保持原样不解析。

        过滤的实际比较使用 ``_parse_finish_time``。
        """
        cutoff = since
        items: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            d = self.list_todo(action="done", page=page)
            batch = d.get("list") or []
            if not batch:
                break
            kept: List[Dict[str, Any]] = []
            stop = False
            for it in batch:
                ft = _parse_finish_time(
                    (it.get("finish_time") or {}).get("finish_time", "")
                )
                if ft is None:
                    kept.append(it)
                    continue
                if ft >= cutoff:
                    kept.append(it)
                else:
                    stop = True
            items.extend(kept)
            if stop or len(batch) < 50:
                break
        return items

    def search_by_mql(
        self, project_key: str, mql: str, session_id: str = ""
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"project_key": project_key, "mql": mql}
        if session_id:
            args["session_id"] = session_id
        contents = self.call_tool("search_by_mql", args)
        return self.first_dict(contents)

    # --- 工作项详情 ---

    def get_workitem_brief(
        self,
        project_key: str,
        work_item_id: Union[str, int],
        work_item_type_key: str,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "project_key": project_key,
            "work_item_id": str(work_item_id),
            "work_item_type_key": work_item_type_key,
        }
        if fields:
            args["fields"] = fields
        contents = self.call_tool("get_workitem_brief", args)
        err = self.detect_error(contents)
        if err:
            raise MeegoMCPError(f"get_workitem_brief 失败: {err}")
        return self.first_dict(contents)

    def get_node_detail(
        self,
        project_key: str,
        work_item_id: Union[str, int],
        work_item_type_key: str,
        node_id: str,
    ) -> Dict[str, Any]:
        contents = self.call_tool(
            "get_node_detail",
            {
                "project_key": project_key,
                "work_item_id": str(work_item_id),
                "work_item_type_key": work_item_type_key,
                "node_id": node_id,
            },
        )
        err = self.detect_error(contents)
        if err:
            raise MeegoMCPError(f"get_node_detail 失败: {err}")
        return self.first_dict(contents)

    # --- 写入：评论 ---

    def add_comment(
        self,
        project_key: str,
        work_item_id: Union[str, int],
        work_item_type_key: str,
        content_markdown: str,
    ) -> Dict[str, Any]:
        """在指定工作项添加一条 markdown 富文本评论。"""
        contents = self.call_tool(
            "add_comment",
            {
                "project_key": project_key,
                "work_item_id": str(work_item_id),
                "work_item_type_key": work_item_type_key,
                "content": content_markdown,
            },
        )
        err = self.detect_error(contents)
        if err:
            raise MeegoMCPError(f"add_comment 失败: {err}")
        return self.first_dict(contents)

    # --- 节点流转 ---

    def get_transition_required(
        self,
        project_key: str,
        work_item_id: Union[str, int],
        state_key: str,
        mode: str = "unfinished",
    ) -> Dict[str, Any]:
        contents = self.call_tool(
            "get_transition_required",
            {
                "project_key": project_key,
                "work_item_id": str(work_item_id),
                "state_key": state_key,
                "mode": mode,
            },
        )
        err = self.detect_error(contents)
        if err:
            raise MeegoMCPError(f"get_transition_required 失败: {err}")
        return self.first_dict(contents)

    def transition_node(
        self,
        project_key: str,
        work_item_id: Union[str, int],
        node_id: str,
        action: str = "confirm",
        rollback_reason: str = "",
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "project_key": project_key,
            "work_item_id": str(work_item_id),
            "node_id": node_id,
            "action": action,
        }
        if rollback_reason:
            args["rollback_reason"] = rollback_reason
        contents = self.call_tool("transition_node", args)
        err = self.detect_error(contents)
        if err:
            raise MeegoMCPError(f"transition_node 失败: {err}")
        return self.first_dict(contents)
