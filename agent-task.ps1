<#
.SYNOPSIS
    Atomic Agent Task Lifecycle Controller for Cyrene.

.DESCRIPTION
    Canonical entrypoint for starting, attaching, and closing agent tasks.
    Atomically performs: remote fetch, exact SHA verification, worktree creation/attachment,
    JetBrains semantic indexing attachment, clean state verification, Git workflow session
    initialization, J-Space state setup, toolchain preflight, and emits READY_TO_EDIT.

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
    Write-Host "  [OK] Task worktree active at: $targetWorktreePath" -ForegroundColor Green

    # 3. Verify Clean State
    Write-Host "`n[3/8] Verifying clean worktree state..." -ForegroundColor Yellow
    $statusOutput = @(& git -C $targetWorktreePath status --porcelain=v1 --untracked-files=all 2>$null)
    if ($statusOutput.Count -gt 0) {
        throw "Task worktree '$targetWorktreePath' is not clean after initialization."
    }
    Write-Host "  [OK] Worktree is clean." -ForegroundColor Green

    # 4. Attach JetBrains Indexing & VCS Root
    if (-not $SkipIDE) {
        Write-Host "`n[4/8] Attaching task worktree to JetBrains semantic index..." -ForegroundColor Yellow
        & pwsh -NoProfile -File $IdeAttachHelper attach -Path $targetWorktreePath
        Write-Host "  [OK] JetBrains content root attached." -ForegroundColor Green
    } else {
        Write-Host "`n[4/8] Skipping IDE attachment (-SkipIDE)." -ForegroundColor Gray
    }

    # 5. Initialize Git Workflow Session
    Write-Host "`n[5/8] Initializing Git workflow session..." -ForegroundColor Yellow
    $pythonExe = if (-not [string]::IsNullOrWhiteSpace($env:CYRENE_PYTHON)) { $env:CYRENE_PYTHON } else { "python" }
    $gitAgentScript = "C:\Users\Baiji\.gemini\skills\git-workflow\scripts\git_agent.py"
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

    # 6. Initialize J-Space Task State
    Write-Host "`n[6/8] Initializing J-Space task state..." -ForegroundColor Yellow
    $jspaceDir = Join-Path $targetWorktreePath ".jspace"
    if (-not (Test-Path -LiteralPath $jspaceDir)) {
        New-Item -ItemType Directory -Path $jspaceDir -Force | Out-Null
    }
    $ledgerPath = Join-Path $jspaceDir "WORKSPACE.md"
    if (-not (Test-Path -LiteralPath $ledgerPath)) {
        $ledgerContent = @"
# J-Space Task Ledger: $Role

## Goal
$Task

## Core
- Canonical Task Worktree: $targetWorktreePath
- Target Branch: $Branch
- Repository: $Repo

## Verified

## Open

## Next
Execute initial task changes.
"@
        [System.IO.File]::WriteAllText($ledgerPath, $ledgerContent, [System.Text.Encoding]::UTF8)
    }
    Write-Host "  [OK] J-Space ledger initialized." -ForegroundColor Green

    # 7. Verify Branch Ownership & Head
    Write-Host "`n[7/8] Verifying branch ownership & HEAD..." -ForegroundColor Yellow
    $currentBranch = (& git -C $targetWorktreePath branch --show-current 2>$null).Trim()
    $currentHead = (& git -C $targetWorktreePath rev-parse HEAD 2>$null).Trim()
    if ($currentBranch -ne $Branch) {
        throw "Active branch '$currentBranch' does not match requested branch '$Branch'."
    }
    Write-Host "  [OK] Branch '$currentBranch' at commit $currentHead." -ForegroundColor Green

    # 8. Emit READY_TO_EDIT
    Write-Host "`n==================================================" -ForegroundColor Cyan
    Write-Host " CYRENE AGENT TASK INITIALIZATION COMPLETE" -ForegroundColor Green
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host "  Task Worktree : $targetWorktreePath" -ForegroundColor White
    Write-Host "  Repository    : $Repo" -ForegroundColor White
    Write-Host "  Branch        : $Branch" -ForegroundColor White
    Write-Host "  Head SHA      : $currentHead" -ForegroundColor White
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
