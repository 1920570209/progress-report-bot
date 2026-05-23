---
name: progress-report-bot
description: >-
  Generate weekly version-progress reports from Feishu Project (Meego, 飞书项目)
  workitems with optional local-git / GitLab / GitHub enrichment, and optionally
  post the rendered Markdown back as a workitem comment that @-mentions owners.
  Use when the user asks about 飞书项目周报 / Meego progress report / 版本进度报告
  / 项目进度对账, when they want to detect 假完成 / 状态滞后 / 延期 / 节点停滞
  between Feishu workitem states and actual git commits/MRs, or when they want
  a merged-to-test PR to auto-transition Feishu workflow nodes (sync). Runs in
  two auto-detected modes: pure-Feishu (zero git config) or git-enhanced
  (local repo / monorepo container / GitLab / GitHub). Output is always local
  Markdown first; pushing comments and transitioning nodes require explicit
  --apply. Invoked via the `progress-report-bot` CLI installed by this skill.
---

# progress-report-bot

Pull workitem progress from Feishu Project (Meego), generate a boss-view weekly
report, and optionally push it back as a `@`-mentioned comment. Optional git
enrichment cross-checks Feishu state with real commits / MRs.

## When to invoke this skill

- User asks for 飞书项目周报 / Meego 周报 / 版本进度 / 项目进度
- User asks to compare 飞书工作项状态 vs 代码实际进度 (假完成 / 状态滞后 / 延期 / 停滞)
- User asks to auto-transition Feishu workflow when PR merged to test branch
- User mentions Meego, 飞书项目, project.feishu.cn MCP

## Quick Start (what the agent should do)

Follow these steps in order. Stop and ask the user only if a required env value
is missing or a destructive `--apply` is requested.

1. **Locate the user's project directory** (where Git history or business
   context lives). `cd` there in a terminal call.
2. **Initialize** (only if `.env` not present in that cwd):
   ```bash
   python -m progress_report_bot init
   ```
   The wizard auto-detects git form (container / single repo / none) and writes
   `.env` next to it. Required env: `MEEGO_MCP_TOKEN`, `MEEGO_PROJECT_KEY`.
3. **Verify connectivity**:
   ```bash
   python -m progress_report_bot ping
   ```
4. **Generate the report locally (safe default — never writes Feishu)**:
   ```bash
   python -m progress_report_bot run-all
   ```
   Produces `data/report.md` + `data/diff.md` + `data/snapshot.json`. Read them
   and surface the headline / risks / discrepancies to the user.
5. **Only when user explicitly confirms**, post the comment back to Feishu:
   ```bash
   python -m progress_report_bot run-all --apply
   ```

## Two run modes (auto-selected by `init`)

- **Pure Feishu mode** (default fallback, `GIT_PROVIDER=none`): analyzes
  workitem flow, owners, delays, stagnant nodes only. No git needed.
- **Git-enhanced mode** (`GIT_PROVIDER=local|gitlab|github`): adds
  commits/MR verification, detects 假完成 / 状态滞后, enables `sync` for
  auto-transitioning Feishu nodes when MR merged to test branch.

`init` picks the right mode:

| cwd shape | result |
|---|---|
| cwd has ≥2 git subdirs (monorepo container) | `local` + `LOCAL_GIT_REPO_ROOT` |
| cwd itself is a git repo | `local` + `LOCAL_GIT_REPO_PATH` |
| no git anywhere | `none` (pure Feishu mode) |

## Commands the agent may run

All commands are safe-by-default. Anything that writes to Feishu requires
`--apply`; without it the command only writes local `data/*.md` files.

```bash
python -m progress_report_bot init                  # one-time wizard, writes ./.env
python -m progress_report_bot ping                  # verify MCP connectivity
python -m progress_report_bot run-all               # local report only (safe, scope=mine)
python -m progress_report_bot run-all --apply       # also post comment to Feishu
python -m progress_report_bot run-all --scope project   # ★ scan whole project (all members)
python -m progress_report_bot diff                  # just the discrepancy report
python -m progress_report_bot sync                  # preview workflow transitions (git mode only)
python -m progress_report_bot sync --apply          # actually transition nodes
python -m progress_report_bot repos                 # diagnose short-code → repo mapping (monorepo)
python -m progress_report_bot fetch-repos           # git fetch all subrepos in container
```

### --scope: who's included in the report

`fetch` / `report` / `push` / `run-all` / `diff` all accept `--scope`:

| value | source | covers |
|---|---|---|
| `mine` (default) | `list_todo` | only the token holder's own workitems (fast, narrow) |
| `project` | `search_by_mql` over `MEEGO_SCAN_TYPES` | the whole project space, all members (slow, broad — boss view) |
| `all` | both, deduped | union of mine + project |

`project` / `all` require `MEEGO_SCAN_TYPES` in `.env` (default `执行需求`). If the
team uses a different workitem type for execution tracking, change it there.

Add `--use-cache` to `report` / `diff` / `push` / `run-all` to reuse the last
`data/snapshot.json` (faster iteration, no Feishu API call).

## Outputs the agent should surface to the user

- `data/report.md` — boss-view weekly summary (completion %, risks, per-demand progress, team contribution)
- `data/diff.md` — discrepancy report (`fake_done`, `lag`, `stagnant_node`, `overdue`, etc.)
- `data/snapshot.json` — raw Feishu + Git data (audit trail)

After running, read these files and quote the headline plus any 🔴 critical
items back to the user. Never paste the full markdown unless asked.

## Safety rules (must follow)

1. **Never run any `--apply` command without explicit user confirmation in
   this turn.** Default to dry-run; print what would change first.
2. **Do not modify the user's `.env` silently.** If a required field is
   missing, prompt the user; do not invent values.
3. **`sync --apply` writes to Feishu workflow state.** Always run `sync`
   without `--apply` first and present the candidate list before asking
   permission.
4. **Respect `SYNC_BRANCH_WHITELIST`** in `.env` — if set, the tool already
   enforces it; do not suggest bypassing it.

## Detailed reference

For full env-var reference, discrepancy taxonomy (9 kinds), demo script and
verified facts about the Feishu MCP API, see [reference.md](reference.md).
