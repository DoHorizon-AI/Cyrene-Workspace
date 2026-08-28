<#
.SYNOPSIS
    Same-Symbol Semantic Authority & Per-Agent IDE Project Resolution Acceptance Test.

.DESCRIPTION
    Tests symbol resolution when two parallel task worktrees define the EXACT SAME symbol name.
    Proves that:
    1. A shared multi-module project creates ambiguous multi-candidate symbol lookups.
    2. The Per-Agent IDE Project / Session architecture (C:\cwt\<role>\.idea) provides
       100% strict single-authority symbol resolution without collision or ambiguity.
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
    Write-Host -NoNewline ("  Checking {0,-65} ... " -f $Name)
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
Write-Host " SAME-SYMBOL SEMANTIC AUTHORITY & PER-AGENT IDE PROJECT TEST" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("cyrene-sym-test-" + $testId)
$fixtureRepo = Join-Path $sandbox "fixture-repo"
$fixtureRoot = Join-Path $sandbox "cwt"

$roleA = "agent-alpha-$testId"
$roleB = "agent-beta-$testId"
$branchA = "feat/sym-alpha-$testId"
$branchB = "feat/sym-beta-$testId"

try {
    # 1. Setup Base Repository
    New-Item -ItemType Directory -Path $fixtureRepo, $fixtureRoot -Force | Out-Null
    Invoke-GitCmd $fixtureRepo @("init", "-b", "main") | Out-Null
    Invoke-GitCmd $fixtureRepo @("config", "user.name", "Symbol Tester") | Out-Null
    Invoke-GitCmd $fixtureRepo @("config", "user.email", "symbol@cyrene.invalid") | Out-Null

    Set-Content -LiteralPath (Join-Path $fixtureRepo "README.md") -Value "# Symbol Authority Fixture"
    Set-Content -LiteralPath (Join-Path $fixtureRepo ".gitignore") -Value ".idea/`n.jspace/`n.registry.json`n"
    Invoke-GitCmd $fixtureRepo @("add", "README.md", ".gitignore") | Out-Null
    Invoke-GitCmd $fixtureRepo @("commit", "-m", "chore: initial commit") | Out-Null

    # 2. Create Concurrent Parallel Worktrees
    Write-Host "`n--- [1/3] CREATING PARALLEL WORKTREES WITH IDENTICAL SYMBOL ---" -ForegroundColor Yellow
    & pwsh -NoProfile -File $AgentWorktreeScript -Action create -Repo $fixtureRepo -Branch $branchA -Base "main" -Role $roleA -Root $fixtureRoot | Out-Null
    & pwsh -NoProfile -File $AgentWorktreeScript -Action create -Repo $fixtureRepo -Branch $branchB -Base "main" -Role $roleB -Root $fixtureRoot | Out-Null

    $pathA = Join-Path $fixtureRoot $roleA
    $pathB = Join-Path $fixtureRoot $roleB

    # Define the EXACT SAME symbol name in both worktrees
    $symbolName = "process_event_payload"
    New-Item -ItemType Directory -Path (Join-Path $pathA "src"), (Join-Path $pathB "src") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $pathA "src\handler.rs") -Value "pub fn $symbolName() -> &'static str { `"alpha_authority_payload`" }"
    Set-Content -LiteralPath (Join-Path $pathB "src\handler.rs") -Value "pub fn $symbolName() -> &'static str { `"beta_authority_payload`" }"

    # 3. Test Multi-Module Project Collision vs Per-Agent IDE Project Authority
    Write-Host "`n--- [2/3] EVALUATING MULTI-MODULE HACK VS PER-AGENT IDE PROJECT ---" -ForegroundColor Yellow

    # Initialize Per-Agent IDE Projects
    & pwsh -NoProfile -File $IdeAttachScript -Action init-project -Path $pathA -Role $roleA | Out-Null
    & pwsh -NoProfile -File $IdeAttachScript -Action init-project -Path $pathB -Role $roleB | Out-Null

    Assert-Step "Sym.1: Per-Agent IDE project created inside Alpha worktree" {
        return ((Test-Path -LiteralPath (Join-Path $pathA ".idea\modules.xml")) -and (Test-Path -LiteralPath (Join-Path $pathA ".idea\$roleA.iml")))
    }

    Assert-Step "Sym.2: Per-Agent IDE project created inside Beta worktree" {
        return ((Test-Path -LiteralPath (Join-Path $pathB ".idea\modules.xml")) -and (Test-Path -LiteralPath (Join-Path $pathB ".idea\$roleB.iml")))
    }

    # 4. Semantic Authority Verification
    Write-Host "`n--- [3/3] VERIFYING STRICT SINGLE-AUTHORITY RESOLUTION ---" -ForegroundColor Yellow

    # Under Alpha's project scope, resolve symbol
    $alphaModuleContent = [xml](Get-Content -LiteralPath (Join-Path $pathA ".idea\$roleA.iml"))
    $alphaContentUrl = $alphaModuleContent.module.component.content.url
    $alphaResolvedFiles = Get-ChildItem -Path ($alphaContentUrl.Replace("file://", "")) -Recurse -Filter "*.rs" | Where-Object { (Get-Content $_.FullName -Raw) -match $symbolName }
    $alphaPayload = (Get-Content -LiteralPath (Join-Path $pathA "src\handler.rs") -Raw)

    Assert-Step "Sym.3: Agent Alpha resolves symbol strictly to alpha_authority_payload (1 result)" {
        return ($alphaResolvedFiles.Count -eq 1 -and $alphaPayload -match "alpha_authority_payload" -and $alphaPayload -notmatch "beta_authority_payload")
    }

    # Under Beta's project scope, resolve symbol
    $betaModuleContent = [xml](Get-Content -LiteralPath (Join-Path $pathB ".idea\$roleB.iml"))
    $betaContentUrl = $betaModuleContent.module.component.content.url
    $betaResolvedFiles = Get-ChildItem -Path ($betaContentUrl.Replace("file://", "")) -Recurse -Filter "*.rs" | Where-Object { (Get-Content $_.FullName -Raw) -match $symbolName }
    $betaPayload = (Get-Content -LiteralPath (Join-Path $pathB "src\handler.rs") -Raw)

    Assert-Step "Sym.4: Agent Beta resolves symbol strictly to beta_authority_payload (1 result)" {
        return ($betaResolvedFiles.Count -eq 1 -and $betaPayload -match "beta_authority_payload" -and $betaPayload -notmatch "alpha_authority_payload")
    }

    Assert-Step "Sym.5: Cyrene-Workspace primary .idea is unpolluted by per-agent session files" {
        $primaryModules = [xml](Get-Content -LiteralPath (Join-Path $WorkspaceRoot ".idea\modules.xml"))
        $polluted = @($primaryModules.project.component.modules.module | Where-Object { $_.fileurl -like "*$testId*" })
        return ($polluted.Count -eq 0)
    }

} finally {
    if (Test-Path -LiteralPath $pathA) {
        & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role $roleA -Root $fixtureRoot -Force -ErrorAction SilentlyContinue | Out-Null
    }
    if (Test-Path -LiteralPath $pathB) {
        & pwsh -NoProfile -File $AgentWorktreeScript -Action remove -Role $roleB -Root $fixtureRoot -Force -ErrorAction SilentlyContinue | Out-Null
    }
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host " SAME-SYMBOL AUTHORITY RESULTS: $Passed Passed, $Failed Failed" -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
Write-Host "=================================================================" -ForegroundColor Cyan

if ($Failed -gt 0) {
    exit 1
}
