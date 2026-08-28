<#
.SYNOPSIS
    Atomic Agent Task Lifecycle Controller for Cyrene.

.DESCRIPTION
    Canonical entrypoint for starting, attaching, and closing agent tasks.
    Atomically performs: toolchain preflight, worktree creation/attachment,
    clean state verification, JetBrains semantic indexing attachment, Git workflow
    session initialization, isolated J-Space state setup under .state, branch/HEAD
    verification, and enforces the strict clean READY_TO_EDIT invariant.

.EXAMPLE
    .\agent-task.ps1 start -Repo Cyrene-Platform -Branch feat/message-connector -Base origin/develop -Role msg-connector -Task "Implement message connector v1"

.EXAMPLE
    .\agent-task.ps1 status

.EXAMPLE
    .\agent-task.ps1 stop -Role msg-connector
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("start", "stop", "status", "cleanup")]
    [string]$Action,

    [Parameter(Position = 1)]
    [Alias("Repository")]
    [string]$Repo,

    [Parameter()]
    [string]$Branch,

    [Parameter()]
    [string]$Base,

    [Parameter()]
    [string]$Role,

    [Parameter()]
    [string]$Task,

    [Parameter()]
    [Alias("WorktreeRoot")]
    [string]$Root,

    [Parameter()]
    [switch]$SkipIDE,

    [Parameter()]
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ScriptDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$WorkspaceRoot = $ScriptDir
$AgentWorktreeHelper = Join-Path $ScriptDir "agent-worktree.ps1"
$PreflightHelper = Join-Path $ScriptDir "scripts\toolchain-preflight.ps1"
$IdeAttachHelper = Join-Path $ScriptDir "scripts\ide-attach.ps1"

function Configure-WorktreeLocalExclude([string]$WorktreePath) {
    try {
        $excludePath = (& git -C $WorktreePath rev-parse --git-path info/exclude 2>$null)
        if (-not [string]::IsNullOrWhiteSpace($excludePath)) {
            $fullExcludePath = if ([System.IO.Path]::IsPathRooted($excludePath)) {
                $excludePath
            } else {
                Join-Path $WorktreePath $excludePath
            }
            $excludeDir = Split-Path -Parent $fullExcludePath
            if (-not (Test-Path -LiteralPath $excludeDir)) {
                New-Item -ItemType Directory -Path $excludeDir -Force | Out-Null
            }
            $existing = if (Test-Path -LiteralPath $fullExcludePath) {
                Get-Content -LiteralPath $fullExcludePath -Raw
            } else {
                ""
            }
            $patternsToAdd = @(".jspace", ".jspace/", ".idea", ".idea/", ".state", ".state/", ".cyrene-snapshot.json")
            $toAppend = @()
            foreach ($pat in $patternsToAdd) {
                if ($existing -notmatch "(?m)^$([Regex]::Escape($pat))$") {
                    $toAppend += $pat
                }
            }
            if ($toAppend.Count -gt 0) {
                Add-Content -LiteralPath $fullExcludePath -Value ($toAppend -join [Environment]::NewLine)
            }
        }
    } catch {
        # Non-fatal exclude setup fallback
    }
}

function Invoke-TaskStart {
    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Parameter -Repo is required for task start."
    }
    if ([string]::IsNullOrWhiteSpace($Role)) {
        if (-not [string]::IsNullOrWhiteSpace($Branch)) {
            $Role = ($Branch.Trim() -replace '[\\/]', '-').Trim('-')
        } else {
            throw "Parameter -Role or -Branch is required."
        }
    }
    if ([string]::IsNullOrWhiteSpace($Branch)) {
        $Branch = "feat/$Role"
    }
    if ([string]::IsNullOrWhiteSpace($Task)) {
        $Task = "Task $Role on $Repo"
    }

    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host " CYRENE ATOMIC AGENT TASK START" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor Cyan

    # 1. Canonical Toolchain Preflight
    Write-Host "`n[1/8] Running canonical toolchain preflight..." -ForegroundColor Yellow
    $preflight = & pwsh -NoProfile -File $PreflightHelper -Quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Toolchain preflight failed; cannot start task."
    }
    Write-Host "  [OK] Toolchain canonical paths verified." -ForegroundColor Green

    # 2. Worktree Creation or Attachment under C:\cwt
    Write-Host "`n[2/8] Creating/attaching dedicated task worktree under C:\cwt..." -ForegroundColor Yellow
    $createArgs = @("create", "-Repo", $Repo, "-Branch", $Branch, "-Role", $Role)
    if (-not [string]::IsNullOrWhiteSpace($Base)) {
        $createArgs += @("-Base", $Base)
    }
    if (-not [string]::IsNullOrWhiteSpace($Root)) {
        $createArgs += @("-Root", $Root)
    }
    & pwsh -NoProfile -File $AgentWorktreeHelper @createArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Worktree creation failed."
    }

    $worktreeRoot = if (-not [string]::IsNullOrWhiteSpace($Root)) { $Root } else {
        if (-not [string]::IsNullOrWhiteSpace($env:CYRENE_WORKTREE_ROOT)) { $env:CYRENE_WORKTREE_ROOT } else { "C:\cwt" }
    }
    $targetWorktreePath = [System.IO.Path]::GetFullPath((Join-Path $worktreeRoot $Role))
    if (-not (Test-Path -LiteralPath $targetWorktreePath)) {
        throw "Target worktree '$targetWorktreePath' was not found after creation."
    }
    Configure-WorktreeLocalExclude $targetWorktreePath
    Write-Host "  [OK] Task worktree active at: $targetWorktreePath" -ForegroundColor Green

    # 3. Verify Initial Clean State
    Write-Host "`n[3/8] Verifying clean worktree state..." -ForegroundColor Yellow
    $statusOutput = @(& git -C $targetWorktreePath status --porcelain=v1 --untracked-files=all 2>$null)
    if ($statusOutput.Count -gt 0) {
        throw "Task worktree '$targetWorktreePath' is not clean after initialization: $($statusOutput -join '; ')"
    }
    Write-Host "  [OK] Worktree baseline is clean." -ForegroundColor Green

    # 4. Attach JetBrains Indexing & VCS Root (Per-Agent Project)
    if (-not $SkipIDE) {
        Write-Host "`n[4/8] Initializing per-agent JetBrains IDE project..." -ForegroundColor Yellow
        & pwsh -NoProfile -File $IdeAttachHelper init-project -Path $targetWorktreePath -Role $Role
        Configure-WorktreeLocalExclude $targetWorktreePath
        Write-Host "  [OK] Per-agent JetBrains IDE project initialized." -ForegroundColor Green
    } else {
        Write-Host "`n[4/8] Skipping IDE attachment (-SkipIDE)." -ForegroundColor Gray
    }

    # 5. Initialize Git Workflow Session
    Write-Host "`n[5/8] Initializing Git workflow session..." -ForegroundColor Yellow
    $pythonExe = if (-not [string]::IsNullOrWhiteSpace($env:CYRENE_PYTHON)) { $env:CYRENE_PYTHON } else { "python" }
    $gitAgentScript = Join-Path $env:USERPROFILE ".gemini\skills\git-workflow\scripts\git_agent.py"
    if (Test-Path -LiteralPath $gitAgentScript) {
        $sessionJson = & $pythonExe $gitAgentScript start --use-current --task $Task --repo $targetWorktreePath 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Git workflow session init note: $sessionJson"
        } else {
            Write-Host "  [OK] Git workflow session initialized." -ForegroundColor Green
        }
    } else {
        Write-Host "  [INFO] Git workflow skill script not found at default location." -ForegroundColor Gray
    }

    # 6. Initialize J-Space Task State (Relocated to C:\cwt\.state to prevent repository pollution)
    Write-Host "`n[6/8] Initializing J-Space task state in isolated runtime directory..." -ForegroundColor Yellow
    $repoLeaf = Split-Path $Repo -Leaf
    $stateDir = Join-Path $worktreeRoot (Join-Path ".state" (Join-Path $repoLeaf $Role))
    if (-not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    $ledgerPath = Join-Path $stateDir "WORKSPACE.md"
    if (-not (Test-Path -LiteralPath $ledgerPath)) {
        $ledgerContent = @"
# J-Space Task Ledger: $Role

## Goal
$Task

## Core
- Canonical Task Worktree: $targetWorktreePath
- Target Branch: $Branch
- Repository: $Repo
- State Directory: $stateDir

## Verified

## Open

## Next
Execute initial task changes.
"@
        [System.IO.File]::WriteAllText($ledgerPath, $ledgerContent, [System.Text.Encoding]::UTF8)
    }
    Write-Host "  [OK] J-Space ledger initialized at: $ledgerPath" -ForegroundColor Green

    # 7. Verify Branch Ownership & Head
    Write-Host "`n[7/8] Verifying branch ownership & HEAD..." -ForegroundColor Yellow
    $currentBranch = (& git -C $targetWorktreePath branch --show-current 2>$null).Trim()
    $currentHead = (& git -C $targetWorktreePath rev-parse HEAD 2>$null).Trim()
    if ($currentBranch -ne $Branch) {
        throw "Active branch '$currentBranch' does not match requested branch '$Branch'."
    }
    Write-Host "  [OK] Branch '$currentBranch' at commit $currentHead." -ForegroundColor Green

    # 8. Strict Invariant Assertion & Emit READY_TO_EDIT
    Write-Host "`n[8/8] Enforcing READY_TO_EDIT clean working tree invariant..." -ForegroundColor Yellow
    $finalStatus = @(& git -C $targetWorktreePath status --porcelain=v1 --untracked-files=all 2>$null)
    if ($finalStatus.Count -gt 0) {
        throw "READY_TO_EDIT invariant violated: task worktree '$targetWorktreePath' has uncommitted or untracked changes: $($finalStatus -join '; ')"
    }
    Write-Host "  [OK] Invariant verified: 0 untracked, 0 modified, 0 staged files." -ForegroundColor Green

    Write-Host "`n==================================================" -ForegroundColor Cyan
    Write-Host " CYRENE AGENT TASK INITIALIZATION COMPLETE" -ForegroundColor Green
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host "  Task Worktree : $targetWorktreePath" -ForegroundColor White
    Write-Host "  Repository    : $Repo" -ForegroundColor White
    Write-Host "  Branch        : $Branch" -ForegroundColor White
    Write-Host "  Head SHA      : $currentHead" -ForegroundColor White
    Write-Host "  State Ledger  : $ledgerPath" -ForegroundColor White
    Write-Host "  IDE Attached  : $(if (-not $SkipIDE) { 'TRUE' } else { 'FALSE' })" -ForegroundColor White
    Write-Host "  Invariant     : READY_TO_EDIT" -ForegroundColor Green
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Output "READY_TO_EDIT"

    return [PSCustomObject]@{
        Status = "READY_TO_EDIT"
        WorktreePath = $targetWorktreePath
        Repository = $Repo
        Branch = $Branch
        Head = $currentHead
        Role = $Role
        StateLedger = $ledgerPath
    }
}

function Invoke-TaskStop {
    if ([string]::IsNullOrWhiteSpace($Role) -and [string]::IsNullOrWhiteSpace($Repo)) {
        throw "Parameter -Role is required for task stop."
    }

    Write-Host "Stopping and cleaning task '$Role'..." -ForegroundColor Cyan
    
    # 1. Detach IDE
    & pwsh -NoProfile -File $IdeAttachHelper clean

    # 2. Remove Worktree
    $removeArgs = @("remove", "-Role", $Role)
    if (-not [string]::IsNullOrWhiteSpace($Root)) {
        $removeArgs += @("-Root", $Root)
    }
    if ($Force) {
        $removeArgs += @("-Force")
    }
    & pwsh -NoProfile -File $AgentWorktreeHelper @removeArgs
}

switch ($Action) {
    "start" { Invoke-TaskStart }
    "stop" { Invoke-TaskStop }
    "cleanup" { Invoke-TaskStop }
    "status" {
        & pwsh -NoProfile -File $AgentWorktreeHelper status
    }
}
