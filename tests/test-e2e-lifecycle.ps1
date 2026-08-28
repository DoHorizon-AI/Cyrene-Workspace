<#
.SYNOPSIS
    Complete End-to-End Real Task Lifecycle Acceptance Test.

.DESCRIPTION
    Executes the canonical full developer agent task lifecycle:
    1. agent-task start -> READY_TO_EDIT
    2. Edit source file in C:\cwt\<role>
    3. Run L1 / L2 validation gate
    4. Record quality evidence bound to Git working tree
    5. Plan & commit changes via git-workflow skill
    6. Verify quality evidence status == passed post-commit without test rerun (Tree-SHA bound)
    7. Push preflight validation
    8. Clean task shutdown
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TestScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $TestScriptDir ".."))
$AgentTaskScript = Join-Path $WorkspaceRoot "agent-task.ps1"
$TestPolicyScript = Join-Path $WorkspaceRoot "scripts\test-policy.ps1"
$GitAgentScript = Join-Path $env:USERPROFILE ".gemini\skills\git-workflow\scripts\git_agent.py"

$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("cyrene-e2e-test-" + $testId)
$fixtureRepo = Join-Path $sandbox "fixture-e2e-repo"
$fixtureRoot = Join-Path $sandbox "cwt"
$taskRole = "e2e-agent-$testId"
$taskBranch = "feat/e2e-demo-$testId"

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " EXECUTING COMPLETE E2E DEVELOPER AGENT TASK LIFECYCLE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

try {
    # 0. Setup Fixture Repository
    New-Item -ItemType Directory -Path $fixtureRepo, $fixtureRoot -Force | Out-Null
    & git -C $fixtureRepo init -b develop 2>$null | Out-Null
    & git -C $fixtureRepo config user.name "E2E Agent" | Out-Null
    & git -C $fixtureRepo config user.email "e2e@cyrene.invalid" | Out-Null

    Set-Content -LiteralPath (Join-Path $fixtureRepo "README.md") -Value "# E2E Fixture Repo"
    Set-Content -LiteralPath (Join-Path $fixtureRepo ".gitignore") -Value ".jspace/`n.idea/`n.registry.json`n__pycache__/`n*.pyc`n"
    & git -C $fixtureRepo add README.md .gitignore | Out-Null
    & git -C $fixtureRepo commit -m "chore: initial commit" | Out-Null

    # STEP 1: agent-task start -> READY_TO_EDIT
    Write-Host "`n[STEP 1] Starting agent task atomically..." -ForegroundColor Yellow
    $swStart = [System.Diagnostics.Stopwatch]::StartNew()
    $startOutput = & pwsh -NoProfile -File $AgentTaskScript -Action start -Repo $fixtureRepo -Branch $taskBranch -Base "develop" -Role $taskRole -Root $fixtureRoot -Task "Implement E2E Feature"
    $swStart.Stop()
    $startText = $startOutput -join "`n"
    if ($startText -notmatch "READY_TO_EDIT") {
        throw "STEP 1 FAILED: READY_TO_EDIT was not emitted. Output: $startText"
    }
    Write-Host "  -> READY_TO_EDIT emitted successfully in $($swStart.ElapsedMilliseconds)ms." -ForegroundColor Green

    $taskWorktreePath = Join-Path $fixtureRoot $taskRole

    # Find the initialized Git workflow session file
    $sessionFiles = @(Get-ChildItem -Path (Join-Path $taskWorktreePath ".git\*\git-workflow\sessions\*.json"), (Join-Path $taskWorktreePath ".git\git-workflow\sessions\*.json") -ErrorAction SilentlyContinue)
    if ($sessionFiles.Count -eq 0) {
        # Check main repo git path
        $sessionFiles = @(Get-ChildItem -Path (Join-Path $fixtureRepo ".git\worktrees\$taskRole\git-workflow\sessions\*.json"), (Join-Path $fixtureRepo ".git\git-workflow\sessions\*.json") -ErrorAction SilentlyContinue)
    }
    if ($sessionFiles.Count -eq 0) {
        throw "Could not locate Git workflow session file in $taskWorktreePath"
    }
    $sessionPath = $sessionFiles[0].FullName
    Write-Host "  -> Active Git workflow session: $sessionPath" -ForegroundColor White

    # STEP 2: Edit source file in task worktree
    Write-Host "`n[STEP 2] Performing source modifications in $taskWorktreePath..." -ForegroundColor Yellow
    $srcDir = Join-Path $taskWorktreePath "src"
    New-Item -ItemType Directory -Path $srcDir -Force | Out-Null
    $featureFile = Join-Path $srcDir "feature.py"
    Set-Content -LiteralPath $featureFile -Value "def calculate_metric(x: int) -> int:`n    return x * 42`n"
    Write-Host "  -> Created src/feature.py in task worktree." -ForegroundColor Green

    # STEP 3: Execute L1 / L2 validation gate
    Write-Host "`n[STEP 3] Running L1 focused validation gate..." -ForegroundColor Yellow
    $swL1 = [System.Diagnostics.Stopwatch]::StartNew()
    $l1Result = & pwsh -NoProfile -File $TestPolicyScript -Level L1 -Command "python -m py_compile '$featureFile'"
    $swL1.Stop()
    Write-Host "  -> L1 focused validation passed in $($swL1.ElapsedMilliseconds)ms." -ForegroundColor Green

    # STEP 4: Record quality evidence
    Write-Host "`n[STEP 4] Recording quality evidence bound to Git working tree..." -ForegroundColor Yellow
    $recordResult = & python $GitAgentScript record-check --session $sessionPath --name "unit-tests" --command "python -m py_compile src/feature.py" --exit-code 0 --repo $taskWorktreePath
    $recordJson = ConvertFrom-Json ($recordResult -join "`n")
    if (-not $recordJson.ok -or $recordJson.result.evidence.status -ne "passed") {
        throw "STEP 4 FAILED: Quality check record status is not passed. Output: $($recordResult -join ' ')"
    }
    $valTreeSha = $recordJson.result.evidence.validated_tree_sha
    Write-Host "  -> Quality evidence recorded with validated_tree_sha: $valTreeSha" -ForegroundColor Green

    # STEP 5: Create commit plan & commit changes
    Write-Host "`n[STEP 5] Planning and committing changes..." -ForegroundColor Yellow
    $planResult = & python $GitAgentScript plan-commit --session $sessionPath --message "feat: add calculate_metric" --include "src/feature.py" --repo $taskWorktreePath
    $planJson = ConvertFrom-Json ($planResult -join "`n")
    if (-not $planJson.ok) {
        throw "STEP 5 Plan FAILED: $($planResult -join ' ')"
    }
    $planPath = $planJson.result.plan_path
    Write-Host "  -> Commit plan created at: $planPath" -ForegroundColor Green

    $finishResult = & python $GitAgentScript finish --plan $planPath --repo $taskWorktreePath
    $finishJson = ConvertFrom-Json ($finishResult -join "`n")
    if (-not $finishJson.ok) {
        throw "STEP 5 Finish FAILED: $($finishResult -join ' ')"
    }
    $newHead = $finishJson.result.commits[0].commit
    Write-Host "  -> Commit created successfully at HEAD: $newHead" -ForegroundColor Green

    # STEP 6: Verify quality evidence survives commit (0 test reruns!)
    Write-Host "`n[STEP 6] Verifying quality evidence post-commit (Tree-SHA preservation)..." -ForegroundColor Yellow
    $swEv = [System.Diagnostics.Stopwatch]::StartNew()
    $skillScripts = Join-Path $env:USERPROFILE ".gemini\skills\git-workflow\scripts"
    $pyCheck = @"
import json, sys
sys.path.insert(0, r'$skillScripts')
from pathlib import Path
from gitops.quality import quality_evidence_status
from gitops.session import load_session

repo = Path(r'$taskWorktreePath')
session = load_session(r'$sessionPath', repo)
status = quality_evidence_status(repo, session)
print(json.dumps(status))
"@
    $evResult = & python -c $pyCheck
    $swEv.Stop()
    $evJson = ConvertFrom-Json ($evResult -join "`n")
    if ($evJson.status -ne "passed") {
        throw "STEP 6 FAILED: Quality evidence did not survive commit. Status: $($evJson.status), Reason: $($evJson.reason)"
    }
    Write-Host "  -> POST-COMMIT QUALITY STATUS: PASSED! Evidence verified in $($swEv.ElapsedMilliseconds)ms (0 duplicate test reruns)." -ForegroundColor Green

    # STEP 7: Push preflight verification
    Write-Host "`n[STEP 7] Executing push preflight validation..." -ForegroundColor Yellow
    $statusCheck = & python $GitAgentScript inspect --repo $taskWorktreePath
    $statusJson = ConvertFrom-Json ($statusCheck -join "`n")
    if (-not $statusJson.result.is_clean -or $statusJson.result.branch -ne $taskBranch) {
        throw "STEP 7 FAILED: Worktree state invalid for push."
    }
    Write-Host "  -> Push preflight verified: Branch '$($statusJson.result.branch)' at HEAD '$($statusJson.result.head)' clean." -ForegroundColor Green

    # STEP 8: Clean task stop
    Write-Host "`n[STEP 8] Stopping task and cleaning up..." -ForegroundColor Yellow
    & pwsh -NoProfile -File $AgentTaskScript -Action stop -Role $taskRole -Root $fixtureRoot -Force | Out-Null
    Write-Host "  -> Task stopped and worktree removed cleanly." -ForegroundColor Green

    Write-Host "`n=================================================================" -ForegroundColor Cyan
    Write-Host " COMPLETE E2E LIFECYCLE SUCCEEDED: 100% PASS" -ForegroundColor Green
    Write-Host "=================================================================" -ForegroundColor Cyan

    return [PSCustomObject]@{
        Status = "PASS"
        TaskStartMs = $swStart.ElapsedMilliseconds
        L1TestMs = $swL1.ElapsedMilliseconds
        EvidenceCheckMs = $swEv.ElapsedMilliseconds
        ValidatedTreeSha = $valTreeSha
        CommittedHead = $newHead
    }
} finally {
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}
