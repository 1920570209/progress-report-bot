"""本地 git 客户端：通过 ``git`` 命令读当前目录的仓库，零远程依赖。

设计动机：
- 让 progress-report-bot 真正可分发为 skill —— 任何人 ``cd`` 到自己的 repo
  目录跑就能用，不用配 token / API URL / 项目路径。
- 牺牲：本地 git 没有 "PR/MR" 概念。我们通过扫 ``git log --merges --grep``
  推断出"合并事件"，包装为 ``PullRequest`` 数据契约（number/url 留空）。

与 GitHubClient / GitLabClient 同构接口：``enabled`` / ``branch_exists`` /
``get_branch_activity`` / ``has_merged_to_targets``。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..models import BranchActivity, GitCommit, PullRequest

logger = logging.getLogger(__name__)


class LocalGitError(RuntimeError):
    pass


# Merge commit subject 形如：
#   Merge branch 'feature-V5.485.0' into 'pre'
#   Merge branch 'feature-V5.485.0' into pre
#   Merge branch 'feature-V5.485.0'                  (合到当前分支)
#   Merge pull request #42 from user/feature-X       (GitHub style)
#   See merge request group/proj!42                  (GitLab MR squash 默认)
_MERGE_SUBJECT_RE = re.compile(
    r"""
    Merge\s+branch\s+'([^']+)'(?:\s+into\s+'?([^'\s]+)'?)?
    | Merge\s+pull\s+request\s+\#(\d+)\s+from\s+\S+/([^\s]+)
    | See\s+merge\s+request\s+\S+\!(\d+)
    """,
    re.VERBOSE,
)


class LocalGitClient:
    """通过本地 ``git`` 命令读取仓库信息。

    参数
    ----
    repo_path : 仓库根目录（包含 ``.git``）。默认 ``cwd``。
    remote_prefix : 查分支时优先用的远端前缀，如 ``origin/``。空串=只看本地分支。
    timeout : 单条 git 命令的超时秒数。
    """

    def __init__(
        self,
        repo_path: str = ".",
        remote_prefix: str = "origin/",
        timeout: float = 15.0,
        repo_root: str = "",
    ) -> None:
        self.default_repo_path = Path(repo_path).resolve() if repo_path else Path.cwd()
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self.remote_prefix = remote_prefix or ""
        self.timeout = timeout
        self._git_bin = shutil.which("git") or "git"
        self._probe_cache: dict = {}

    # 兼容旧调用：未指定 repo 时使用 default
    @property
    def repo_path(self) -> Path:
        return self.default_repo_path

    def _select_path(self, repo: Optional[str]) -> Path:
        """根据 repo 参数选择实际工作目录。"""
        if repo:
            p = Path(repo)
            if p.exists():
                return p.resolve()
        return self.default_repo_path

    # ----------------------------------------------------------
    # 基础能力
    # ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        # 单仓库模式：default_repo_path 本身是 git
        if self._is_git_repo(self.default_repo_path):
            return True
        # 多仓库容器模式：repo_root 下任意子目录是 git 即视为已启用
        if self.repo_root and self.repo_root.exists():
            try:
                for child in self.repo_root.iterdir():
                    if child.is_dir() and (child / ".git").exists():
                        logger.info(
                            "Local git ready (container mode): root=%s, remote_prefix=%s",
                            self.repo_root,
                            self.remote_prefix or "(local)",
                        )
                        return True
            except OSError:
                pass
        return False

    def _is_git_repo(self, path: Path) -> bool:
        key = str(path)
        if key in self._probe_cache:
            return self._probe_cache[key]
        if not path.exists():
            self._probe_cache[key] = False
            return False
        try:
            out = self._run(["rev-parse", "--is-inside-work-tree"], cwd=path)
            ok = out.strip() == "true"
        except LocalGitError:
            ok = False
        self._probe_cache[key] = ok
        if ok and key == str(self.default_repo_path):
            logger.info(
                "Local git ready: repo=%s, remote_prefix=%s",
                path,
                self.remote_prefix or "(local)",
            )
        return ok

    def _run(self, args: List[str], cwd: Optional[Path] = None) -> str:
        wd = cwd or self.default_repo_path
        cmd = [self._git_bin, *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(wd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as te:
            raise LocalGitError(f"git timeout: {' '.join(args)}") from te
        except FileNotFoundError as fe:
            raise LocalGitError(f"未找到 git 可执行文件: {fe}") from fe
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:200]
            raise LocalGitError(
                f"git {' '.join(args)} (code={proc.returncode}): {stderr}"
            )
        return proc.stdout or ""

    # ----------------------------------------------------------
    # 分支解析
    # ----------------------------------------------------------

    def _resolve_ref(self, branch: str, cwd: Optional[Path] = None) -> Optional[str]:
        """优先 origin/{branch}（远端最新），fallback 本地分支。返回可用的 ref。"""
        if self.remote_prefix:
            cand = f"{self.remote_prefix}{branch}"
            if self._verify(cand, cwd=cwd):
                return cand
        if self._verify(branch, cwd=cwd):
            return branch
        if "/" in branch and self._verify(branch, cwd=cwd):
            return branch
        return None

    def _verify(self, ref: str, cwd: Optional[Path] = None) -> bool:
        try:
            self._run(["rev-parse", "--verify", "--quiet", ref], cwd=cwd)
            return True
        except LocalGitError:
            return False

    def branch_exists(self, repo: str, branch: str) -> bool:
        wd = self._select_path(repo)
        if not self._is_git_repo(wd):
            return False
        return self._resolve_ref(branch, cwd=wd) is not None

    # ----------------------------------------------------------
    # commits / merge-commit
    # ----------------------------------------------------------

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 分隔符避免与 commit subject 内字符冲突
    _LOG_SEP = "<<P_R_B_SEP>>"
    _LOG_FMT = f"%H{_LOG_SEP}%h{_LOG_SEP}%s{_LOG_SEP}%an{_LOG_SEP}%aI"

    def list_commits(
        self,
        ref: str,
        since: Optional[datetime] = None,
        limit: int = 100,
        cwd: Optional[Path] = None,
    ) -> List[GitCommit]:
        args = [
            "log",
            ref,
            f"--pretty=format:{self._LOG_FMT}",
            f"--max-count={limit}",
        ]
        if since:
            args.append(f"--since={self._iso(since)}")
        try:
            out = self._run(args, cwd=cwd)
        except LocalGitError as e:
            logger.debug("list_commits 失败 %s: %s", ref, e)
            return []
        commits: List[GitCommit] = []
        for line in out.splitlines():
            parts = line.split(self._LOG_SEP)
            if len(parts) < 5:
                continue
            full_sha, short_sha, subject, author, iso = parts[0:5]
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                dt = datetime.now()
            commits.append(
                GitCommit(
                    sha=short_sha or full_sha[:8],
                    message=subject[:120],
                    author=author,
                    date=dt,
                    url="",
                )
            )
        return commits

    def _merge_commits_into(
        self,
        target_ref: str,
        source_branch_pattern: str,
        limit: int = 50,
        cwd: Optional[Path] = None,
    ) -> List[GitCommit]:
        """在 ``target_ref`` 的历史里找含某 source 名的 merge commit。"""
        args = [
            "log",
            target_ref,
            "--merges",
            f"--grep=Merge branch '{source_branch_pattern}'",
            f"--pretty=format:{self._LOG_FMT}",
            f"--max-count={limit}",
        ]
        try:
            out = self._run(args, cwd=cwd)
        except LocalGitError:
            return []
        commits: List[GitCommit] = []
        for line in out.splitlines():
            parts = line.split(self._LOG_SEP)
            if len(parts) < 5:
                continue
            full_sha, short_sha, subject, author, iso = parts[0:5]
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                dt = datetime.now()
            commits.append(
                GitCommit(
                    sha=short_sha or full_sha[:8],
                    message=subject,
                    author=author,
                    date=dt,
                )
            )
        return commits

    @classmethod
    def _parse_merge_subject(
        cls, subject: str
    ) -> Optional[dict]:
        m = _MERGE_SUBJECT_RE.search(subject)
        if not m:
            return None
        # 优先 `Merge branch 'X' into Y`
        if m.group(1):
            return {"head": m.group(1), "base": m.group(2) or "", "number": 0}
        if m.group(3):  # GitHub PR
            return {"head": m.group(4), "base": "", "number": int(m.group(3))}
        if m.group(5):  # GitLab MR squash
            return {"head": "", "base": "", "number": int(m.group(5))}
        return None

    # ----------------------------------------------------------
    # 业务封装
    # ----------------------------------------------------------

    def get_branch_activity(
        self,
        repo: str,
        branch: str,
        since: Optional[datetime] = None,
        target_branches: Optional[List[str]] = None,
    ) -> BranchActivity:
        wd = self._select_path(repo)
        if not self._is_git_repo(wd):
            return BranchActivity(repo=str(wd), branch=branch, exists=False)

        ref = self._resolve_ref(branch, cwd=wd)
        activity = BranchActivity(
            repo=str(wd),
            branch=branch,
            exists=ref is not None,
        )
        if ref is None:
            return activity

        activity.commits = self.list_commits(ref, since=since, cwd=wd)

        seen: set = set()
        for tgt in (target_branches or []):
            tgt_ref = self._resolve_ref(tgt, cwd=wd)
            if tgt_ref is None:
                continue
            for mc in self._merge_commits_into(tgt_ref, branch, limit=20, cwd=wd):
                key = mc.sha
                if key in seen:
                    continue
                seen.add(key)
                parsed = self._parse_merge_subject(mc.message) or {}
                activity.pull_requests.append(
                    PullRequest(
                        number=int(parsed.get("number") or 0),
                        title=mc.message[:120],
                        state="merged",
                        author=mc.author,
                        head_branch=str(parsed.get("head") or branch),
                        base_branch=tgt,
                        url="",
                        merged=True,
                    )
                )

            if not any(p.base_branch == tgt for p in activity.pull_requests):
                if self._is_ancestor(ref, tgt_ref, cwd=wd):
                    activity.pull_requests.append(
                        PullRequest(
                            number=0,
                            title=f"(fast-forward/squash) {branch} → {tgt}",
                            state="merged",
                            author="",
                            head_branch=branch,
                            base_branch=tgt,
                            url="",
                            merged=True,
                        )
                    )
        return activity

    def has_merged_to_targets(
        self,
        repo: str,
        branch: str,
        target_branches: List[str],
    ) -> Optional[PullRequest]:
        if not target_branches:
            return None
        wd = self._select_path(repo)
        if not self._is_git_repo(wd):
            return None
        ref = self._resolve_ref(branch, cwd=wd)
        if ref is None:
            return None
        for tgt in target_branches:
            tgt_ref = self._resolve_ref(tgt, cwd=wd)
            if tgt_ref is None:
                continue
            for mc in self._merge_commits_into(tgt_ref, branch, limit=5, cwd=wd):
                parsed = self._parse_merge_subject(mc.message) or {}
                return PullRequest(
                    number=int(parsed.get("number") or 0),
                    title=mc.message[:120],
                    state="merged",
                    author=mc.author,
                    head_branch=branch,
                    base_branch=tgt,
                    url="",
                    merged=True,
                )
            if self._is_ancestor(ref, tgt_ref, cwd=wd):
                return PullRequest(
                    number=0,
                    title=f"(fast-forward/squash) {branch} → {tgt}",
                    state="merged",
                    author="",
                    head_branch=branch,
                    base_branch=tgt,
                    url="",
                    merged=True,
                )
        return None

    def _is_ancestor(self, ancestor: str, descendant: str, cwd: Optional[Path] = None) -> bool:
        wd = cwd or self.default_repo_path
        try:
            subprocess.run(
                [self._git_bin, "merge-base", "--is-ancestor", ancestor, descendant],
                cwd=str(wd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
