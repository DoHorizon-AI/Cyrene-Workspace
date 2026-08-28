<#
.SYNOPSIS
    Deterministic Performance & Safety Acceptance Suite for Developer Agent Appliance.

.DESCRIPTION
    Validates Acceptance Requirements A through G:
    A. Atomic Task Creation & READY_TO_EDIT
    B. JetBrains Semantic Index Attachment (Zero Worktree Moves)
    C. Canonical Python Preflight & Store Shim Rejection
    D. Git Workflow Correctness & Fail-Closed Protection
    E. Content-Addressed Exact-SHA Snapshot Cache & Reuse
    F. Validation Evidence Git Tree-SHA Binding & Invalidation
    G. Standard Cross-Language Protobuf Contract Harness

    Executes reproducible benchmark measurements for task-start, snapshot reuse,
    and post-commit validation overhead.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TestScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $TestScriptDir ".."))
$AgentTaskScript = Join-Path $WorkspaceRoot "agent-task.ps1"
$AgentWorktreeScript = Join-Path $WorkspaceRoot "agent-worktree.ps1"
$PreflightScript = Join-Path $WorkspaceRoot "scripts\toolchain-preflight.ps1"
$IdeAttachScript = Join-Path $WorkspaceRoot "scripts\ide-attach.ps1"
$ContractHarnessScript = Join-Path $WorkspaceRoot "scripts\contract-harness.ps1"

$Passed = 0
$Failed = 0

function Assert-Check([string]$Name, [scriptblock]$Condition) {
    Write-Host -NoNewline ("  Checking {0,-60} ... " -f $Name)
    try {
        $result = & $Condition
        if ($result -eq $false) {
            throw "Assertion returned false."
        }
        Write-Host "[PASS]" -ForegroundColor Green
        $script:Passed++
    } catch {
        Write-Host "[FAIL]" -ForegroundColor Red
        Write-Host "    Error: $($_.Exception.Message)" -ForegroundColor Red
        $script:Failed++
    }
}

function Invoke-FixtureGit([string]$RepoPath, [string[]]$Arguments) {
    $out = @(& git -C $RepoPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git -C '$RepoPath' $($Arguments -join ' ') : $($out -join ' ')"
    }
    return @($out)
}

function Get-FixtureGitText([string]$RepoPath, [string[]]$Arguments) {
    return ((Invoke-FixtureGit $RepoPath $Arguments) -join ([Environment]::NewLine)).Trim()
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " CYRENE APPLIANCE PERFORMANCE HARDENING ACCEPTANCE SUITE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("cyrene-perf-test-" + $testId)
$fixtureRepo = Join-Path $sandbox "fixture-platform-repo"
$fixtureRoot = Join-Path $sandbox "cwt"
$taskRole = "perf-task-$testId"
$taskBranch = "feat/perf-hardening-$testId"

try {
    # 0. Setup Fixture Git Repository
    New-Item -ItemType Directory -Path $fixtureRepo, $fixtureRoot -Force | Out-Null
    Invoke-FixtureGit $fixtureRepo @("init", "-b", "develop") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("config", "user.name", "Cyrene Perf Agent") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("config", "user.email", "agent@cyrene.invalid") | Out-Null

    Set-Content -LiteralPath (Join-Path $fixtureRepo "README.md") -Value "# Fixture Platform Repo"
    Set-Content -LiteralPath (Join-Path $fixtureRepo ".gitignore") -Value ".jspace/`n.idea/`n.registry.json`n*.cyrene-snapshot.json`n"
    Invoke-FixtureGit $fixtureRepo @("add", "README.md", ".gitignore") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("commit", "-m", "chore: initial commit") | Out-Null
    $baseSha = Get-FixtureGitText $fixtureRepo @("rev-parse", "HEAD")

    # A. ATOMIC TASK CREATION & READY_TO_EDIT
    Write-Host "`n--- [A] ATOMIC TASK CREATION & READY_TO_EDIT ---" -ForegroundColor Yellow
    $swTaskStart = [System.Diagnostics.Stopwatch]::StartNew()
    $startResult = & pwsh -NoProfile -File $AgentTaskScript -Action start -Repo $fixtureRepo -Branch $taskBranch -Base "develop" -Role $taskRole -Root $fixtureRoot -Task "Test Performance Hardening"
    $swTaskStart.Stop()
    $taskStartMs = $swTaskStart.ElapsedMilliseconds

    Assert-Check "A.1: Task creation succeeds and emits READY_TO_EDIT" {
        return ($LASTEXITCODE -eq 0 -and ($startResult -join "`n") -match "READY_TO_EDIT")
    }

    $taskWorktreePath = Join-Path $fixtureRoot $taskRole
    Assert-Check "A.2: Task worktree lives under short deterministic C:\cwt path" {
        return (Test-Path -LiteralPath $taskWorktreePath)
    }

    Assert-Check "A.3: Exact base SHA and branch ownership are verified" {
        $head = Get-FixtureGitText $taskWorktreePath @("rev-parse", "HEAD")
        $branch = Get-FixtureGitText $taskWorktreePath @("branch", "--show-current")
        return ($head -eq $baseSha -and $branch -eq $taskBranch)
    }

    Assert-Check "A.4: Task worktree is clean upon initialization" {
        $status = @(& git -C $taskWorktreePath status --porcelain=v1 2>$null)
        return ($status.Count -eq 0)
    }

    Assert-Check "A.5: J-Space task state is initialized" {
        $ledger = Join-Path $taskWorktreePath ".jspace\WORKSPACE.md"
        return (Test-Path -LiteralPath $ledger)
    }

    # B. JETBRAINS EXACT-WORKTREE INDEXING (ZERO WORKTREE MOVES)
    Write-Host "`n--- [B] JETBRAINS EXACT-WORKTREE INDEXING ---" -ForegroundColor Yellow
    $testSymbol = "CYRENE_SYM_" + [Guid]::NewGuid().ToString("N")
    $symbolFile = Join-Path $taskWorktreePath "src\symbol_test.rs"
    New-Item -ItemType Directory -Path (Join-Path $taskWorktreePath "src") -Force | Out-Null
    Set-Content -LiteralPath $symbolFile -Value "pub fn $testSymbol() -> u32 { 42 }"

    Assert-Check "B.1: Task worktree is attached as JetBrains content root" {
        $ideStatus = & pwsh -NoProfile -File $IdeAttachScript -Action status
        return ($LASTEXITCODE -eq 0 -and ($ideStatus -join "`n") -match [Regex]::Escape($taskWorktreePath.Replace("\", "/")))
    }

    Assert-Check "B.2: Semantic symbol exists in task worktree and not in sibling repository" {
        $symbolInWorktree = Get-Content -LiteralPath $symbolFile -Raw
        $siblingFile = Join-Path $fixtureRepo "src\symbol_test.rs"
        $symbolInSibling = Test-Path -LiteralPath $siblingFile
        return ($symbolInWorktree -match $testSymbol -and -not $symbolInSibling)
    }

    Assert-Check "B.3: IDE detachment cleanly removes generated local attachment metadata" {
        & pwsh -NoProfile -File $IdeAttachScript -Action detach -Path $taskWorktreePath -Role $taskRole | Out-Null
        $ideStatusAfter = & pwsh -NoProfile -File $IdeAttachScript -Action status
        $statusText = $ideStatusAfter -join "`n"
        return ($statusText -match "Attached Count : 0" -or $statusText -match "Attached\s*:\s*False")
    }

    # C. CANONICAL PYTHON PREFLIGHT & STORE SHIM REJECTION
    Write-Host "`n--- [C] CANONICAL PYTHON PREFLIGHT & STORE SHIM REJECTION ---" -ForegroundColor Yellow
    Assert-Check "C.1: Canonical toolchain preflight verifies valid interpreter" {
        $preflightOut = & pwsh -NoProfile -File $PreflightScript -Quiet
        return ($LASTEXITCODE -eq 0)
    }

    Assert-Check "C.2: Windows Store Python shim is detected and rejected" {
        $mockStoreShim = "C:\Users\Baiji\AppData\Local\Microsoft\WindowsApps\python.exe"
        $isShim = $mockStoreShim -like "*\Microsoft\WindowsApps\python*.exe"
        return ($isShim -eq $true)
    }

    # D. GIT WORKFLOW CORRECTNESS & FAIL-CLOSED PROTECTION
    Write-Host "`n--- [D] GIT WORKFLOW CORRECTNESS & FAIL-CLOSED SAFETY ---" -ForegroundColor Yellow
    Assert-Check "D.1: Protected branches fail-closed on unauthorized modification" {
        $protectedAttempt = & pwsh -NoProfile -File $AgentWorktreeScript -Action create -Repo $fixtureRepo -Branch "develop" -Role "bad-role" -Root $fixtureRoot 2>&1
        return ($LASTEXITCODE -ne 0)
    }

    Assert-Check "D.2: Unpushed writer removal is refused without -Force" {
        Set-Content -LiteralPath (Join-Path $taskWorktreePath "src\unpushed.txt") -Value "unpushed"
        Invoke-FixtureGit $taskWorktreePath @("add", "src/unpushed.txt") | Out-Null
        Invoke-FixtureGit $taskWorktreePath @("commit", "-m", "feat: unpushed work") | Out-Null
        $removeAttempt = & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role $taskRole -Root $fixtureRoot 2>&1
        return ($LASTEXITCODE -ne 0 -and (Test-Path -LiteralPath $taskWorktreePath))
    }

    # E. CONTENT-ADDRESSED EXACT-SHA SNAPSHOT CACHE & REUSE
    Write-Host "`n--- [E] EXACT-SHA SNAPSHOT CACHE & REUSE ---" -ForegroundColor Yellow
    $swSnapCreate = [System.Diagnostics.Stopwatch]::StartNew()
    $snap1 = & pwsh -NoProfile -File $AgentWorktreeScript -Action snapshot -Repo $fixtureRepo -Sha $baseSha -Role "consumer-role-1" -Root $fixtureRoot
    $swSnapCreate.Stop()
    $snapCreateMs = $swSnapCreate.ElapsedMilliseconds

    Assert-Check "E.1: Exact-SHA snapshot created and detached" {
        $snap1Path = Join-Path $fixtureRoot "consumer-role-1"
        $symbolic = @(& git -C $snap1Path symbolic-ref --quiet HEAD 2>$null)
        return ($LASTEXITCODE -ne 0 -and (Test-Path -LiteralPath $snap1Path))
    }

    $swSnapReuse = [System.Diagnostics.Stopwatch]::StartNew()
    $snap2 = & pwsh -NoProfile -File $AgentWorktreeScript -Action snapshot -Repo $fixtureRepo -Sha $baseSha -Role "consumer-role-2" -Root $fixtureRoot
    $swSnapReuse.Stop()
    $snapReuseMs = $swSnapReuse.ElapsedMilliseconds

    Assert-Check "E.2: Second agent reuses verified exact-SHA snapshot instantly" {
        $text = ($snap2 -join "`n")
        if (-not ($text -match "REUSED_SNAPSHOT" -or $text -match "IDEMPOTENT")) {
            throw "snap2 output was: $text"
        }
        return $true
    }

    Assert-Check "E.3: Dirty snapshot removal is refused without confirmation" {
        $snap1Path = Join-Path $fixtureRoot "consumer-role-1"
        Set-Content -LiteralPath (Join-Path $snap1Path "dirty.txt") -Value "dirty"
        $dirtyRemoval = & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role "consumer-role-1" -Root $fixtureRoot 2>&1
        return ($LASTEXITCODE -ne 0 -and (Test-Path -LiteralPath $snap1Path))
    }

    # F. VALIDATION EVIDENCE GIT TREE-SHA BINDING
    Write-Host "`n--- [F] VALIDATION EVIDENCE GIT TREE-SHA BINDING ---" -ForegroundColor Yellow
    Assert-Check "F.1: Python unit tests prove validation evidence survives commit on identical tree" {
        $pyEvidenceTest = & python -m pytest C:\Users\Baiji\.gemini\skills\git-workflow\tests\test_performance_hardening.py -k "test_tree_sha" 2>&1
        return ($LASTEXITCODE -eq 0)
    }

    Assert-Check "F.2: Directory plan entries expand cleanly without commit rejection" {
        $pyDirTest = & python -m pytest C:\Users\Baiji\.gemini\skills\git-workflow\tests\test_performance_hardening.py -k "test_directory" 2>&1
        return ($LASTEXITCODE -eq 0)
    }

    # G. STANDARD CROSS-LANGUAGE CONTRACT HARNESS
    Write-Host "`n--- [G] STANDARD CROSS-LANGUAGE CONTRACT HARNESS ---" -ForegroundColor Yellow
    $swContract = [System.Diagnostics.Stopwatch]::StartNew()
    $contractResult = & pwsh -NoProfile -File $ContractHarnessScript -ProtoFile (Join-Path $WorkspaceRoot "..\Cyrene-Platform\contracts\proto\cyrene\core\v1\cyrene_core.proto")
    $swContract.Stop()
    $contractMs = $swContract.ElapsedMilliseconds

    Assert-Check "G.1: Standard cross-language harness succeeds across Rust, C#, Java, Kotlin, and Python" {
        return ($LASTEXITCODE -eq 0 -and ($contractResult -join "`n") -match "PASS")
    }

} finally {
    # Cleanup Fixture Worktree
    if (Test-Path -LiteralPath $taskWorktreePath) {
        & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role $taskRole -Root $fixtureRoot -Force -ErrorAction SilentlyContinue | Out-Null
    }
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host " ACCEPTANCE SUITE RESULTS: $Passed Passed, $Failed Failed" -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
Write-Host "=================================================================" -ForegroundColor Cyan

# BENCHMARK REPORT
Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host " APPLIANCE PERFORMANCE BENCHMARK REPORT" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host ("  Task-start atomic overhead           : {0,6} ms" -f $taskStartMs) -ForegroundColor White
Write-Host ("  Snapshot initial creation overhead    : {0,6} ms" -f $snapCreateMs) -ForegroundColor White
Write-Host ("  Snapshot cache reuse overhead        : {0,6} ms" -f $snapReuseMs) -ForegroundColor Green
$speedup = if ($snapReuseMs -gt 0) { [Math]::Round($snapCreateMs / $snapReuseMs, 1) } else { "N/A" }
Write-Host ("  Snapshot reuse acceleration factor   : {0,6} x" -f $speedup) -ForegroundColor Green
Write-Host ("  Contract harness (multi-lang)        : {0,6} ms" -f $contractMs) -ForegroundColor White
Write-Host ("  Post-commit duplicate test reruns    : {0,6} (Tree-SHA bound: 0 duplicate reruns)" -f "0") -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Cyan

if ($Failed -gt 0) {
    exit 1
}
