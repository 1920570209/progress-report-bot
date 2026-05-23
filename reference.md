# progress-report-bot · Reference

Detailed reference material loaded on demand by the SKILL. Keep `SKILL.md`
focused on triggers and the happy path; put exhaustive tables here.

## .env reference

Use `python -m progress_report_bot init` to generate this interactively.
Manually edit only when changing defaults.

| Variable | Required | Purpose |
|---|---|---|
| `MEEGO_MCP_TOKEN` | yes | Feishu Project MCP auth (`X-Mcp-Token` header) |
| `MEEGO_PROJECT_KEY` | yes | Feishu space key (24-char hex), e.g. `xxxxxxxxxxxxxxxxxxxxxxxx` |
| `MEEGO_REPORT_CARRIER_ID` | for `--apply` | Workitem ID that receives the rendered comment |
| `MEEGO_REPORT_CARRIER_TYPE_KEY` | for `--apply` | Workitem type key of the carrier |
| `MEEGO_FOCUS_WORK_ITEM_ID` | optional | Limit `sync` to one workitem (demo/debug) |
| `GIT_PROVIDER` | optional | `none` (default) / `local` / `gitlab` / `github` |
| `LOCAL_GIT_REPO_PATH` | `local` single-repo | Path to a git repo (default `.`) |
| `LOCAL_GIT_REPO_ROOT` | `local` monorepo | Container dir whose subdirs are git repos |
| `REPO_ID_MAP` | `local` monorepo | Feishu short-code → subdir, e.g. `&3neskoa5d=A8-cloudsum-server-script` |
| `LOCAL_GIT_REMOTE_PREFIX` | `local` | Branch lookup prefix, default `origin/` |
| `GITLAB_TOKEN` / `GITLAB_API_BASE` / `GITLAB_DEFAULT_PROJECT` | `gitlab` | Remote API mode |
| `GITHUB_TOKEN` / `GITHUB_DEFAULT_REPO` | `github` | Remote API mode |
| `MERGE_TARGET_BRANCHES` | `sync` | Branches considered "merged to test", comma-sep, default `pre` |
| `SYNC_BRANCH_WHITELIST` | `sync` safety | Only these branches may trigger sync; empty = no restriction |
| `SYNC_SOURCE_NODE_NAME` | `sync` | Feishu node to transition from, default `功能开发` |
| `SYNC_TARGET_NODE_NAMES` | `sync` | Acceptable next nodes, default `功能测试,提测,测试中` |
| `REPORT_WINDOW_DAYS` | optional | Default 7 |

## Discrepancy taxonomy (F6)

The `diff` command classifies every workitem into at most one kind:

### Pure-Feishu rules (always active)

| kind | severity | trigger | meaning |
|---|---|---|---|
| `stagnant_node` | 🔴 critical (>256d) / 🟡 warning (>30d) | Node entered N days ago, still not done | PM lost track / blocked |
| `overdue` | 🟡 warning | `is_delayed = true` from Feishu | Past due date |

### Git-enhanced rules (only when `GIT_PROVIDER != none`)

| kind | severity | trigger | meaning |
|---|---|---|---|
| `fake_done` | 🔴 critical | Feishu done + 0 commit + 0 merged MR in window | Status flipped, code unchanged |
| `lag` | 🔴 critical | Feishu at `功能开发` + Git already merged to test branch | Should have advanced |
| `branch_not_found` | 🟡 warning | Feishu branch field set + branch missing in git | Typo / deleted / unpushed |
| `lead` | 🟡 warning | Feishu at `功能测试` + nothing merged yet | Status ahead of code |
| `missing_branch` | 🟡 warning | At dev node, branch field empty | Field not filled in |
| `no_repo` | 🟡 warning | Branch set, but `选择仓库` field empty | Repo selection missing |
| `stale_branch` | 🔵 info | Branch exists, 0 commit in window | Likely abandoned |

## Demo (3 min)

1. `python -m progress_report_bot run-all` — local md only, show terminal preview
2. Open the Feishu carrier workitem comment area (requires `--apply`)
3. `python -m progress_report_bot sync` — list candidates that can auto-transition
4. `sync --apply` — confirm Feishu node state changes in real time

## Verified facts about the Feishu MCP API

- Meego MCP Server v1.0.0, ~51 tools, `X-Mcp-Token` header auth
- 开发分支 custom field key: `field_1946d0` (samples: `feature-V5.485.0`, `heikesong_test`)
- 选择仓库 custom field key: `field_8f07fb`, value is a JSON array of short-codes (e.g. `&3neskoa5d`)
- Built-in fields used as-is: `is_delayed`, `assignees.owners`
- Node transition uses `transition_node` with `action=confirm`

## Local-git mode internals

- Branches resolved with `git rev-parse --verify origin/{branch}` then local fallback
- Commits via `git log --since`
- "MR" inferred from `git log --merges --grep` (subjects match GitHub, GitLab and plain merge styles)
- Merge detection via `git merge-base --is-ancestor` for fast-forward / squash merges
- Monorepo: each workitem's `选择仓库` short-codes are mapped to subdirs of `LOCAL_GIT_REPO_ROOT` via `REPO_ID_MAP`, and the tool aggregates commits/MR across all matched repos

## Sync triple guardrail

`sync --apply` only fires when **all three** pass:

1. `--apply` flag is explicit (default is dry-run)
2. The workitem's branch is in `SYNC_BRANCH_WHITELIST` (empty = unrestricted, use with care)
3. That branch has actually merged into `MERGE_TARGET_BRANCHES`
