# progress-report-bot

> 一个为 **Cursor / Claude Code** 设计的 Agent Skill：让 AI 直接帮你跑飞书项目（Meego）的版本进度周报、做 飞书↔Git 对账、按需把评论 `@` 回飞书工作项。
>
> 默认安全：所有命令只生成本地 `data/*.md`，写入飞书必须显式 `--apply`。

---

## Install (as an Agent Skill)

### 1. Clone

```bash
git clone https://github.com/1920570209/progress-report-bot.git
cd progress-report-bot
```

### 2. Run the installer

**Windows (PowerShell)**

```powershell
./scripts/install-skill.ps1
```

**macOS / Linux**

```bash
chmod +x ./scripts/install-skill.sh
./scripts/install-skill.sh
```

The installer does two things:

1. `pip install -e .` — so the `progress-report-bot` CLI and `python -m progress_report_bot` work from any directory.
2. Creates a junction / symlink from the user-level skill dirs to this repo:

   ```
   ~/.cursor/skills/progress-report-bot      ->  <this repo>
   ~/.claude/skills/progress-report-bot      ->  <this repo>
   ```

   Cursor and Claude Code both auto-discover skills in these locations on the next start.

> Want a project-pinned skill (shared via the repo where you analyze code)? Run `./scripts/install-skill.ps1 -ProjectScope` (PowerShell) or `./scripts/install-skill.sh --project` (bash). It will link into `<cwd>/.cursor/skills/` and `<cwd>/.claude/skills/` instead of the home dir.

### 3. Restart Cursor / Claude

After restart, the assistant will see this skill's `SKILL.md` (with its YAML frontmatter) and trigger it whenever the user asks about 飞书项目周报 / 版本进度 / 进度对账 / Meego progress / etc.

### 4. Try it — **全程对话，只手输 token**

打开你要分析的目录（可以是单个 git 仓库，也可以是一个**包含多个 git 子仓库的总目录**），在 Cursor / Claude Code 里直接说：

| 你说什么 | AI 会做什么 |
|---|---|
| "跑一下这个项目的飞书周报" | ① 让你手输 `MEEGO_MCP_TOKEN`（**唯一手输项**）→ ② 调 `projects --json` 列出可访问的项目空间，让你**选序号** → ③ 列出 scope（mine/project/all）让你选 → ④ 自动探测 git（总目录下多个子仓库会**全部扫描**）→ ⑤ `run-all` 生成本地报告 → 摘要贴回对话 |
| "看一下飞书 vs Git 对账，有没有假完成的" | 直接 `diff`，把🔴项贴回来 |
| "把这份周报发到飞书评论区" | 调 `carriers --json` 列出**你参与的工作项**让你选序号 → 写入 `.env` → 再次确认后 `run-all --apply` |
| "切到全员视角再跑一遍" | `--scope project` 重跑 |
| "PR 已合到 test 了，把对应飞书节点推进一下" | `sync` dry-run 列候选 → 等你确认 → `sync --apply` |

**AI 不会跑 `init` 向导**——所有"选哪个"都在对话里以编号列表呈现，AI 帮你写 `.env`。

如果你想手工跑（无 Cursor 环境）：

```bash
cd <your-project>
python -m progress_report_bot init        # 交互向导（人手版，会问 token + 选项目 + 选承载工作项）
python -m progress_report_bot run-all     # safe: local md only
```

---

## What this skill actually does

| Capability | Command | Writes to Feishu? |
|---|---|---|
| Pull workitems + render boss-view weekly report | `run-all` | ❌ (local md only) |
| Same, plus post `@`-mention comment to a Feishu workitem | `run-all --apply` | ✅ (explicit) |
| Just the Feishu↔Git discrepancy report (假完成 / 状态滞后 / 节点停滞) | `diff` | ❌ |
| When a PR is merged to test branch, auto-advance the Feishu node | `sync --apply` | ✅ (explicit) |
| One-time interactive setup wizard (auto-detects git form) | `init` | — |

Two run modes, **auto-detected by `init`**:

- **Pure Feishu mode** — no git anywhere, only needs the MCP token & project key
- **Git-enhanced mode** — `local` (zero token, scans your repo / monorepo container), `gitlab`, or `github`

See [SKILL.md](SKILL.md) for the agent-facing trigger contract, and [reference.md](reference.md) for the full env-var table, discrepancy taxonomy, and verified facts about the Feishu MCP API.

---

## Manual install (without the installer script)

If you don't want the install script to touch your home dir, you can do it by hand:

```bash
pip install -e .

# Cursor (Windows example)
cmd /c mklink /J "%USERPROFILE%\.cursor\skills\progress-report-bot" "%CD%"
# macOS / Linux
ln -s "$(pwd)" ~/.cursor/skills/progress-report-bot
ln -s "$(pwd)" ~/.claude/skills/progress-report-bot
```

Then restart your editor.

---

## Uninstall

Delete the symlinks; `pip uninstall progress-report-bot` for the CLI:

```bash
rm ~/.cursor/skills/progress-report-bot
rm ~/.claude/skills/progress-report-bot
pip uninstall progress-report-bot
```

On Windows, use `Remove-Item` on the junctions instead of `rm`.

---

## Configuration

Configuration lives in a `.env` file in **the directory you `cd` into to run the bot** (so each project keeps its own setup). The `init` wizard generates it for you; full reference in [reference.md](reference.md).

Minimal `.env`:

```env
MEEGO_MCP_TOKEN=m-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
MEEGO_PROJECT_KEY=your-feishu-project-key
MEEGO_REPORT_CARRIER_ID=             # leave blank if you only want local md
GIT_PROVIDER=none                    # init wizard auto-fills this
```

---

## License

MIT — see [LICENSE](LICENSE).
