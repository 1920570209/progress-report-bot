#!/usr/bin/env bash
# Install progress-report-bot as a Cursor / Claude Agent Skill (and CLI).
#
# Usage:
#   ./scripts/install-skill.sh                # personal scope (default)
#   ./scripts/install-skill.sh --project      # install into ./.cursor/skills + ./.claude/skills of cwd
#   ./scripts/install-skill.sh --no-pip       # skip `pip install -e .`
#
# After running, restart Cursor / Claude to pick up the new skill.

set -euo pipefail

SKILL_NAME="progress-report-bot"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROJECT_SCOPE=0
NO_PIP=0
for arg in "$@"; do
  case "$arg" in
    --project) PROJECT_SCOPE=1 ;;
    --no-pip)  NO_PIP=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "==> progress-report-bot · skill installer"
echo "    repo: $REPO_ROOT"

# 1) pip install -e .
echo
if [[ "$NO_PIP" -eq 0 ]]; then
  echo "[1/2] pip install -e ."
  ( cd "$REPO_ROOT" && python -m pip install -e . )
else
  echo "[1/2] pip install -e .  (skipped, --no-pip)"
fi

# 2) Link into skill dirs
echo
echo "[2/2] Register as Agent Skill"

if [[ "$PROJECT_SCOPE" -eq 1 ]]; then
  CWD="$(pwd)"
  TARGETS=(
    "$CWD/.cursor/skills/$SKILL_NAME"
    "$CWD/.claude/skills/$SKILL_NAME"
  )
else
  TARGETS=(
    "$HOME/.cursor/skills/$SKILL_NAME"
    "$HOME/.claude/skills/$SKILL_NAME"
  )
fi

link_one() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  if [[ -e "$target" || -L "$target" ]]; then
    if [[ -L "$target" ]]; then
      rm "$target"
    else
      echo "   ! $target already exists as a real dir; skipping (delete it manually if you want to relink)" >&2
      return 1
    fi
  fi
  ln -s "$REPO_ROOT" "$target"
  echo "   + $target  ->  $REPO_ROOT"
  return 0
}

ok=0
for t in "${TARGETS[@]}"; do
  if link_one "$t"; then ok=$((ok+1)); fi
done

echo
echo "==> done. Linked $ok / ${#TARGETS[@]} skill targets."
echo
echo "Next steps:"
echo "  1. Restart Cursor / Claude to pick up the new skill."
echo "  2. cd into the project you want to analyze."
echo "  3. Run:  python -m progress_report_bot init"
echo
