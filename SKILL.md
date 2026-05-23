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

## Agent interaction contract (IMPORTANT)

**All configuration choices happen in the Cursor chat — NOT in a local terminal
wizard.** Do NOT run `python -m progress_report_bot init` for end users.

The user only **types** one secret by hand: **`MEEGO_MCP_TOKEN`**. Every other
parameter is presented as a **numbered list in chat**; the user replies with a
number (or "skip").

### Step 1 — pick the working directory

`cd` into the directory the user wants analyzed. This is where `.env` and `data/`
live.

**Git container rule:** if cwd has **one or more child directories each
containing `.git`**, treat cwd as a monorepo container — set
`LOCAL_GIT_REPO_ROOT=<cwd>` and scan **all** sub-repos (do not pick just one).
If cwd itself is the only git repo, use `LOCAL_GIT_REPO_PATH=<cwd>`.

Detect by listing cwd (no command needed):

| cwd shape | `.env` git settings |
|---|---|
| ≥1 child dirs with `.git` | `GIT_PROVIDER=local`, `LOCAL_GIT_REPO_ROOT=<cwd>` |
| cwd itself has `.git`, no sub-repos | `GIT_PROVIDER=local`, `LOCAL_GIT_REPO_PATH=<cwd>` |
| no git | `GIT_PROVIDER=none` |

When `LOCAL_GIT_REPO_ROOT` is set, git enrich scans **every** sub-repo under
that root (even if a workitem's「选择仓库」field is empty).

### Step 2 — ask ONLY for the token, then discover the rest

1. **Ask the user once:** `MEEGO_MCP_TOKEN` (Feishu Project → settings → MCP).
2. Write a minimal `./.env` with just the token (+ default URL):
   ```env
   MEEGO_MCP_URL=https://project.feishu.cn/mcp_server/v1
   MEEGO_MCP_TOKEN=<from user>
   ```
3. Verify:
   ```bash
   python -m progress_report_bot ping
   ```

### Step 3 — let the user pick the project (in chat)

```bash
python -m progress_report_bot projects --json
```

Parse the JSON array. Present a **numbered list** in chat, e.g.:

```
请选择飞书项目空间：
1. XX产品 (key=abc..., simple=xx)
2. YY迭代 (key=def..., simple=yy)
```

Wait for the user's reply. Then patch `.env` with `MEEGO_PROJECT_KEY` and
`MEEGO_SPACE_SIMPLE_NAME`.

### Step 4 — let the user pick scope (in chat)

Present options (default = 1):

```
请选择采集范围：
1. mine — 只看本人参与的工作项（快）
2. project — 扫整个空间（老板周报视角）
3. all — 本人 + 全空间合并
```

Write `DEFAULT_SCOPE` to `.env`.

If user picks **project** or **all**, also let them pick workitem types:

```bash
python -m progress_report_bot types --project-key <key> --json
```

Present numbered list → write `MEEGO_SCAN_TYPES` (comma-sep type names).

### Step 5 — generate report locally (safe, no Feishu write)

Finish `.env` with git auto-detection (Step 1) and defaults:

```env
MEEGO_REPORT_CARRIER_ID=
MEEGO_REPORT_CARRIER_TYPE_KEY=
MERGE_TARGET_BRANCHES=test
SYNC_SOURCE_NODE_NAME=功能开发
SYNC_TARGET_NODE_NAMES=功能测试,提测,测试中
SYNC_BRANCH_WHITELIST=
REPORT_WINDOW_DAYS=7
LOCAL_GIT_REMOTE_PREFIX=origin/
```

Run:

```bash
python -m progress_report_bot run-all
```

Read `data/report.md` and `data/diff.md`. Reply with:

- one-line headline (completion %, delayed count, risk count)
- top 3 critical discrepancies if any
- 3–5 most relevant `@`-mentioned owners

Do NOT paste full markdown unless asked.

### Step 6 — push to Feishu comment (only on explicit request)

Require explicit "post" / "send" / "推送" / "发评论" in **this turn**.

**Do NOT run `--apply` until the user also picks a carrier workitem in chat.**

1. Fetch candidates (only workitems **this user participates in**):

   ```bash
   python -m progress_report_bot carriers --project-key <key> --json
   ```

2. Present numbered list in chat:

   ```
   请选择接收周报评论的工作项（仅列出你参与的）：
   1. #12345 [功能开发] 版本 V5.485 周报承载
   2. #67890 [测试中] 迭代总结
   0. 跳过（只保留本地 md）
   ```

3. After user picks, patch `.env`:

   ```env
   MEEGO_REPORT_CARRIER_ID=<id>
   MEEGO_REPORT_CARRIER_TYPE_KEY=<type_key from JSON>
   ```

4. Confirm once more, then:

   ```bash
   python -m progress_report_bot run-all --apply
   ```

Never use `--select-carrier` (terminal interactive) in Agent mode — always
present choices in chat and write `.env`.

## Two run modes (auto-detected, no init wizard)

- **Pure Feishu mode** (`GIT_PROVIDER=none`): workitem flow, owners, delays only.
- **Git-enhanced mode** (`GIT_PROVIDER=local|gitlab|github`): adds commit/MR
  cross-check, `fake_done` / `lag` detection, and `sync` for node transitions.

## Commands the agent may run

Safe by default. Feishu writes need `--apply` + user confirmation.

```bash
# Discovery (token only)
python -m progress_report_bot ping
python -m progress_report_bot projects --json

# Discovery (token + --project-key)
python -m progress_report_bot types --project-key <key> --json
python -m progress_report_bot carriers --project-key <key> --json

# Report pipeline
python -m progress_report_bot run-all                          # local md only
python -m progress_report_bot run-all --scope project          # boss view
python -m progress_report_bot run-all --apply                  # post comment (carrier must be in .env)
python -m progress_report_bot diff
python -m progress_report_bot sync                             # preview node transitions
python -m progress_report_bot sync --apply
python -m progress_report_bot repos                            # monorepo mapping diagnose
python -m progress_report_bot fetch-repos                      # git fetch all sub-repos
```

**Do NOT run:** `python -m progress_report_bot init` — that is a local terminal
wizard; end users configure through chat instead.

### --scope

| value | covers |
|---|---|
| `mine` (default) | token holder's own workitems via `list_todo` |
| `project` | whole space via MQL over `MEEGO_SCAN_TYPES` |
| `all` | union of both, deduped |

Add `--use-cache` to skip re-fetching Feishu when iterating on the same snapshot.

## Outputs to surface

- `data/report.md` — boss-view weekly summary
- `data/diff.md` — discrepancy report
- `data/snapshot.json` — raw audit trail

## Safety rules

1. **Never `--apply` without explicit user confirmation in this turn.**
2. **Only ask the user to type the MCP token.** Everything else = numbered choice in chat.
3. **Carrier workitem must come from `carriers --json`** — only items the user participates in.
4. **`sync --apply` changes Feishu workflow.** Always dry-run `sync` first.
5. **Do not invent project keys, workitem IDs, or type keys.**

## Detailed reference

See [reference.md](reference.md) for env-var table, discrepancy taxonomy, and MCP API facts.
