#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Install progress-report-bot as a Cursor / Claude Agent Skill (and CLI).

.DESCRIPTION
    Run this from the repo root after `git clone`. It will:
      1. pip install -e .  (so `python -m progress_report_bot` works anywhere)
      2. Create a junction / symlink from the user's skills dirs to this repo,
         so SKILL.md is auto-discovered by Cursor and Claude.

    Skill targets (created if the parent dir exists):
      - $HOME\.cursor\skills\progress-report-bot   (Cursor personal skill)
      - $HOME\.claude\skills\progress-report-bot   (Claude Code personal skill)

.PARAMETER ProjectScope
    If set, install into <repo>\.cursor\skills and <repo>\.claude\skills of
    the *current* git project (i.e. cwd when you ran the script) instead of
    the personal home dirs. Useful for sharing a repo-pinned skill.

.PARAMETER NoPip
    Skip the `pip install -e .` step (use if you have your own venv setup).

.EXAMPLE
    ./scripts/install-skill.ps1
    ./scripts/install-skill.ps1 -ProjectScope
#>

[CmdletBinding()]
param(
    [switch]$ProjectScope,
    [switch]$NoPip
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$skillName = "progress-report-bot"

Write-Host "==> progress-report-bot · skill installer" -ForegroundColor Cyan
Write-Host "    repo: $repoRoot"

# 1) pip install -e .
if (-not $NoPip) {
    Write-Host ""
    Write-Host "[1/2] pip install -e ." -ForegroundColor Cyan
    Push-Location $repoRoot
    try {
        & python -m pip install -e . 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed (exit $LASTEXITCODE)"
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "[1/2] pip install -e .  (skipped, -NoPip)" -ForegroundColor DarkGray
}

# 2) Link into skill dirs
Write-Host ""
Write-Host "[2/2] Register as Agent Skill" -ForegroundColor Cyan

if ($ProjectScope) {
    $cwd = Get-Location
    $targets = @(
        (Join-Path $cwd ".cursor\skills\$skillName"),
        (Join-Path $cwd ".claude\skills\$skillName")
    )
} else {
    $targets = @(
        (Join-Path $HOME ".cursor\skills\$skillName"),
        (Join-Path $HOME ".claude\skills\$skillName")
    )
}

function New-SkillLink {
    param([string]$Target)

    $parent = Split-Path -Parent $Target
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if (Test-Path $Target) {
        $item = Get-Item $Target -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Remove-Item $Target -Force
        } else {
            Write-Host "   ! $Target already exists as a real dir; skipping (delete it manually if you want to relink)" -ForegroundColor Yellow
            return $false
        }
    }
    try {
        New-Item -ItemType Junction -Path $Target -Value $repoRoot -ErrorAction Stop | Out-Null
        Write-Host "   + $Target  →  $repoRoot"
        return $true
    } catch {
        Write-Host "   ! Junction failed ($_); falling back to copy" -ForegroundColor Yellow
        Copy-Item -Recurse -Force $repoRoot $Target
        return $true
    }
}

$ok = 0
foreach ($t in $targets) { if (New-SkillLink -Target $t) { $ok++ } }

Write-Host ""
Write-Host "==> done. Linked $ok / $($targets.Count) skill targets." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart Cursor / Claude to pick up the new skill."
Write-Host "  2. cd into the project you want to analyze."
Write-Host "  3. Run:  python -m progress_report_bot init"
Write-Host ""
