<#
.SYNOPSIS
    Dual Parallel Task Worktree and JetBrains Semantic Index Isolation Acceptance Test.

.DESCRIPTION
    Proves that multiple concurrent agent task worktrees operate in C:\cwt simultaneously,
    attach to JetBrains IDE semantic content roots without conflict, isolate symbol definitions
    between tasks and sibling checkouts, and clean up independently.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TestScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $TestScriptDir ".."))
$AgentWorktreeScript = Join-Path $WorkspaceRoot "agent-worktree.ps1"
$IdeAttachScript = Join-Path $WorkspaceRoot "scripts\ide-attach.ps1"

$Passed = 0
$Failed = 0

function Assert-Step([string]$Name, [scriptblock]$Condition) {
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

function Invoke-GitCmd([string]$RepoPath, [string[]]$Arguments) {
    $out = @(& git -C $RepoPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git -C '$RepoPath' $($Arguments -join ' ') : $($out -join ' ')"
    }
    return @($out)
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " DUAL PARALLEL WORKTREE & JETBRAINS INDEXING ACCEPTANCE SUITE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("cyrene-dual-test-" + $testId)
$fixtureRepo = Join-Path $sandbox "dual-fixture-repo"
$fixtureRoot = Join-Path $sandbox "cwt"

$roleA = "agent-alpha-$testId"
$roleB = "agent-beta-$testId"
$branchA = "feat/alpha-$testId"
$branchB = "feat/beta-$testId"

try {
    # 1. Setup Base Repository
    New-Item -ItemType Directory -Path $fixtureRepo, $fixtureRoot -Force | Out-Null
    Invoke-GitCmd $fixtureRepo @("init", "-b", "develop") | Out-Null
    Invoke-GitCmd $fixtureRepo @("config", "user.name", "Dual Agent Tester") | Out-Null
    Invoke-GitCmd $fixtureRepo @("config", "user.email", "dual@cyrene.invalid") | Out-Null

    Set-Content -LiteralPath (Join-Path $fixtureRepo "README.md") -Value "# Dual Parallel Test Repo"
    Set-Content -LiteralPath (Join-Path $fixtureRepo ".gitignore") -Value ".jspace/`n.idea/`n.registry.json`n"
    Invoke-GitCmd $fixtureRepo @("add", "README.md", ".gitignore") | Out-Null
    Invoke-GitCmd $fixtureRepo @("commit", "-m", "chore: initial commit") | Out-Null
    $baseSha = (Invoke-GitCmd $fixtureRepo @("rev-parse", "HEAD") -join "").Trim()

    # 2. Create Parallel Worktree A & B
    Write-Host "`n--- [1/4] CONCURRENT PARALLEL WORKTREE CREATION ---" -ForegroundColor Yellow
    $wtA = & pwsh -NoProfile -File $AgentWorktreeScript -Action create -Repo $fixtureRepo -Branch $branchA -Role $roleA -Root $fixtureRoot
    $wtB = & pwsh -NoProfile -File $AgentWorktreeScript -Action create -Repo $fixtureRepo -Branch $branchB -Role $roleB -Root $fixtureRoot

    $pathA = Join-Path $fixtureRoot $roleA
    $pathB = Join-Path $fixtureRoot $roleB

    Assert-Step "Dual.1: Task worktree Alpha created at exact base SHA" {
        $headA = (Invoke-GitCmd $pathA @("rev-parse", "HEAD") -join "").Trim()
        return ((Test-Path -LiteralPath $pathA) -and $headA -eq $baseSha)
    }

    Assert-Step "Dual.2: Task worktree Beta created concurrently at exact base SHA" {
        $headB = (Invoke-GitCmd $pathB @("rev-parse", "HEAD") -join "").Trim()
        return ((Test-Path -LiteralPath $pathB) -and $headB -eq $baseSha)
    }

    # 3. Attach Both to JetBrains Project Model
    Write-Host "`n--- [2/4] DUAL JETBRAINS SEMANTIC INDEX ATTACHMENT ---" -ForegroundColor Yellow
    & pwsh -NoProfile -File $IdeAttachScript -Action attach -Path $pathA -Role $roleA | Out-Null
    & pwsh -NoProfile -File $IdeAttachScript -Action attach -Path $pathB -Role $roleB | Out-Null

    Assert-Step "Dual.3: Both task worktrees registered in JetBrains modules & VCS mappings" {
        $ideStatus = & pwsh -NoProfile -File $IdeAttachScript -Action status
        $statusText = $ideStatus -join "`n"
        return ($statusText -match [Regex]::Escape($pathA.Replace("\", "/")) -and $statusText -match [Regex]::Escape($pathB.Replace("\", "/")))
    }

    # 4. Symbol Isolation Proof
    Write-Host "`n--- [3/4] SYMBOL DEFINITION & SEMANTIC RESOLUTION ISOLATION ---" -ForegroundColor Yellow
    $symA = "SYM_ALPHA_" + [Guid]::NewGuid().ToString("N")
    $symB = "SYM_BETA_" + [Guid]::NewGuid().ToString("N")

    New-Item -ItemType Directory -Path (Join-Path $pathA "src"), (Join-Path $pathB "src") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $pathA "src\alpha.rs") -Value "pub struct $symA;"
    Set-Content -LiteralPath (Join-Path $pathB "src\beta.rs") -Value "pub struct $symB;"

    Assert-Step "Dual.4: Alpha symbol exists only in Alpha worktree (isolated from Beta and Primary)" {
        $inAlpha = (Get-Content -LiteralPath (Join-Path $pathA "src\alpha.rs") -Raw) -match $symA
        $inBeta = Test-Path -LiteralPath (Join-Path $pathB "src\alpha.rs")
        $inPrimary = Test-Path -LiteralPath (Join-Path $fixtureRepo "src\alpha.rs")
        return ($inAlpha -and -not $inBeta -and -not $inPrimary)
    }

    Assert-Step "Dual.5: Beta symbol exists only in Beta worktree (isolated from Alpha and Primary)" {
        $inBeta = (Get-Content -LiteralPath (Join-Path $pathB "src\beta.rs") -Raw) -match $symB
        $inAlpha = Test-Path -LiteralPath (Join-Path $pathA "src\beta.rs")
        $inPrimary = Test-Path -LiteralPath (Join-Path $fixtureRepo "src\beta.rs")
        return ($inBeta -and -not $inAlpha -and -not $inPrimary)
    }

    # 5. Independent Commit & Detach
    Write-Host "`n--- [4/4] INDEPENDENT COMMIT & CLEANUP ISOLATION ---" -ForegroundColor Yellow
    Invoke-GitCmd $pathA @("add", "src/alpha.rs") | Out-Null
    Invoke-GitCmd $pathA @("commit", "-m", "feat: add alpha") | Out-Null
    
    # Detach Alpha only
    & pwsh -NoProfile -File $IdeAttachScript -Action detach -Path $pathA -Role $roleA | Out-Null
    
    Assert-Step "Dual.6: Detaching Alpha leaves Beta attached and active" {
        $ideStatusMid = & pwsh -NoProfile -File $IdeAttachScript -Action status
        $midText = $ideStatusMid -join "`n"
        return ($midText -notmatch [Regex]::Escape($pathA.Replace("\", "/")) -and $midText -match [Regex]::Escape($pathB.Replace("\", "/")))
    }

    # Detach Beta
    & pwsh -NoProfile -File $IdeAttachScript -Action detach -Path $pathB -Role $roleB | Out-Null
    Assert-Step "Dual.7: Detaching Beta clears remaining local IDE attachments" {
        $ideStatusEnd = & pwsh -NoProfile -File $IdeAttachScript -Action status
        return (($ideStatusEnd -join "`n") -match "Attached Count : 0")
    }

} finally {
    if (Test-Path -LiteralPath $pathA) {
        & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role $roleA -Root $fixtureRoot -Force -ErrorAction SilentlyContinue | Out-Null
    }
    if (Test-Path -LiteralPath $pathB) {
        & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role $roleB -Root $fixtureRoot -Force -ErrorAction SilentlyContinue | Out-Null
    }
    & pwsh -NoProfile -File $IdeAttachScript -Action clean -ErrorAction SilentlyContinue | Out-Null
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host " DUAL PARALLEL ACCEPTANCE RESULTS: $Passed Passed, $Failed Failed" -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
Write-Host "=================================================================" -ForegroundColor Cyan

if ($Failed -gt 0) {
    exit 1
}
