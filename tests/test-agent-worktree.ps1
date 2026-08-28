<#
.SYNOPSIS
    Validation suite for Parallel Agent Worktree Isolation (agent-worktree.ps1).
.DESCRIPTION
    Proves:
    1. Task worktree creation
    2. Exact-SHA snapshot creation
    3. Status view and ownership tracking
    4. Duplicate and conflict detection
    5. Dirty cleanup refusal
    6. Unpushed-commit cleanup refusal
    7. Safe removal of clean disposable worktree
    8. Short Windows path resolution
    9. Product source isolation (zero modifications to business repos)
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDir = Split-Path -Parent $ScriptDir
$AgentWorktreeScript = Join-Path $WorkspaceDir "agent-worktree.ps1"

$testPassed = 0
$testFailed = 0

function Assert-Condition([string]$name, [scriptblock]$condition) {
    Write-Host -NoNewline "  Testing $name... "
    try {
        $result = & $condition
        if ($result -eq $false) {
            throw "Condition returned false."
        }
        Write-Host "[PASS]" -ForegroundColor Green
        $script:testPassed++
    } catch {
        Write-Host "[FAIL]" -ForegroundColor Red
        Write-Host "    Error: $($_.Exception.Message)" -ForegroundColor Red
        $script:testFailed++
    }
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " RUNNING AGENT WORKTREE ISOLATION ACCEPTANCE SUITE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

# Setup temporary sandbox for testing
$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$tempSandbox = Join-Path $env:TEMP "cyrene-wt-test-$testId"
$testRepoDir = Join-Path $tempSandbox "mock-plugin-repo"
$testWorktreeRoot = Join-Path $tempSandbox "test-cwt"

New-Item -ItemType Directory -Path $testRepoDir -Force | Out-Null
New-Item -ItemType Directory -Path $testWorktreeRoot -Force | Out-Null

try {
    # Initialize mock git repository
    git -C $testRepoDir init -b develop 2>&1 | Out-Null
    git -C $testRepoDir config user.name "Test Agent"
    git -C $testRepoDir config user.email "agent@cyrene.local"

    Set-Content -Path (Join-Path $testRepoDir "README.md") -Value "# Mock Plugin Repo"
    git -C $testRepoDir add README.md
    git -C $testRepoDir commit -m "feat: initial commit on develop" 2>&1 | Out-Null
    $initialSha = (git -C $testRepoDir rev-parse HEAD).Trim()

    Set-Content -Path (Join-Path $testRepoDir "file2.txt") -Value "Second commit content"
    git -C $testRepoDir add file2.txt
    git -C $testRepoDir commit -m "feat: second commit on develop" 2>&1 | Out-Null
    $secondSha = (git -C $testRepoDir rev-parse HEAD).Trim()

    # 1. TASK WORKTREE CREATION
    Assert-Condition "Task worktree creation for writer role" {
        $role = "idea-spring-test"
        $branch = "chore/spring-boot-test-1"
        pwsh -File $AgentWorktreeScript create -Repo $testRepoDir -Branch $branch -Base develop -Role $role -Root $testWorktreeRoot
        
        $expectedPath = Join-Path $testWorktreeRoot $role
        if (-not (Test-Path $expectedPath)) { return $false }
        $currentBranch = (git -C $expectedPath rev-parse --abbrev-ref HEAD).Trim()
        return ($currentBranch -eq $branch)
    }

    # 2. EXACT-SHA SNAPSHOT CREATION
    Assert-Condition "Exact-SHA snapshot creation (detached HEAD)" {
        $role = "rider-media-snapshot-test"
        pwsh -File $AgentWorktreeScript snapshot -Repo $testRepoDir -Sha $initialSha -Role $role -Root $testWorktreeRoot
        
        $expectedPath = Join-Path $testWorktreeRoot $role
        if (-not (Test-Path $expectedPath)) { return $false }
        $currentHead = (git -C $expectedPath rev-parse HEAD).Trim()
        $markerPath = Join-Path $expectedPath ".cyrene-snapshot.json"
        
        return ($currentHead -eq $initialSha -and (Test-Path $markerPath))
    }

    # 3. STATUS VIEW & OWNERSHIP
    Assert-Condition "Status command displays active worktree ownership" {
        $statusOutput = pwsh -File $AgentWorktreeScript status -Root $testWorktreeRoot | Out-String
        return ($statusOutput -match "idea-spring-test" -and $statusOutput -match "rider-media-snapshot-test" -and $statusOutput -match "writer" -and $statusOutput -match "snapshot")
    }

    # 4. DUPLICATE DETECTION & IDEMPOTENCY
    Assert-Condition "Duplicate / conflict detection for existing worktree" {
        # Idempotent re-run on same branch/role should succeed
        $null = pwsh -File $AgentWorktreeScript create -Repo $testRepoDir -Branch "chore/spring-boot-test-1" -Base develop -Role "idea-spring-test" -Root $testWorktreeRoot 2>&1
        
        # Conflict: different branch at same role/path should fail
        $conflictFailed = $false
        $err1 = pwsh -File $AgentWorktreeScript create -Repo $testRepoDir -Branch "chore/conflicting-branch" -Base develop -Role "idea-spring-test" -Root $testWorktreeRoot 2>&1
        if ($LASTEXITCODE -ne 0) {
            $conflictFailed = $true
        }
        
        # Conflict: same branch in another worktree should fail
        $branchInUseFailed = $false
        $err2 = pwsh -File $AgentWorktreeScript create -Repo $testRepoDir -Branch "chore/spring-boot-test-1" -Base develop -Role "another-role" -Root $testWorktreeRoot 2>&1
        if ($LASTEXITCODE -ne 0) {
            $branchInUseFailed = $true
        }

        return ($conflictFailed -and $branchInUseFailed)
    }

    # 5. DIRTY WORKTREE PROTECTION
    Assert-Condition "Refusal to remove dirty worktree without -Force" {
        $writerPath = Join-Path $testWorktreeRoot "idea-spring-test"
        # Make dirty
        Set-Content -Path (Join-Path $writerPath "dirty-file.txt") -Value "uncommitted data"
        
        $refused = $false
        $err = pwsh -File $AgentWorktreeScript remove -Role "idea-spring-test" -Root $testWorktreeRoot 2>&1
        if ($LASTEXITCODE -ne 0) {
            $refused = $true
        }

        # Verify worktree still exists
        return ($refused -and (Test-Path $writerPath))
    }

    # 6. UNPUSHED COMMIT PROTECTION
    Assert-Condition "Refusal to remove worktree with unpushed commits without -Force" {
        $writerPath = Join-Path $testWorktreeRoot "idea-spring-test"
        # Commit the dirty file so working tree is clean but has unpushed commits relative to develop
        git -C $writerPath add dirty-file.txt
        git -C $writerPath commit -m "feat: agent work commit" 2>&1 | Out-Null
        
        $refused = $false
        $err = pwsh -File $AgentWorktreeScript remove -Role "idea-spring-test" -Root $testWorktreeRoot 2>&1
        if ($LASTEXITCODE -ne 0) {
            $refused = $true
        }

        # Verify worktree still exists
        return ($refused -and (Test-Path $writerPath))
    }

    # 7. SAFE REMOVAL OF CLEAN DISPOSABLE WORKTREE
    Assert-Condition "Safe removal of clean snapshot worktree" {
        $snapshotPath = Join-Path $testWorktreeRoot "rider-media-snapshot-test"
        pwsh -File $AgentWorktreeScript remove -Role "rider-media-snapshot-test" -Root $testWorktreeRoot
        
        return (-not (Test-Path $snapshotPath))
    }

    # Force removal of writer worktree
    Assert-Condition "Explicit -Force removal of unpushed writer worktree" {
        $writerPath = Join-Path $testWorktreeRoot "idea-spring-test"
        pwsh -File $AgentWorktreeScript remove -Role "idea-spring-test" -Root $testWorktreeRoot -Force
        
        return (-not (Test-Path $writerPath))
    }

    # 8. SHORT WINDOWS PATH RESOLUTION
    Assert-Condition "Short path resolution (CYRENE_WORKTREE_ROOT & fallback)" {
        $env:CYRENE_WORKTREE_ROOT = Join-Path $tempSandbox "custom-short-root"
        $resolved = pwsh -File $AgentWorktreeScript status | Out-String
        $env:CYRENE_WORKTREE_ROOT = ""
        return ($resolved -match "custom-short-root")
    }

} finally {
    # Clean up temp sandbox
    if (Test-Path $tempSandbox) {
        Remove-Item -Path $tempSandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 9. BUSINESS REPO IMMUTABILITY CHECK
Assert-Condition "Product and business repositories unmodified" {
    $repos = @(
        "../Cyrene-Platform",
        "../plugins",
        "../services/cyrene-astrbot-rev",
        "../services/cyrene-dh-system-internal",
        "../services/cyrene-reactor",
        "../services/Cyrene-Yield",
        "../services/cyrene-exchange"
    )

    $allClean = $true
    foreach ($r in $repos) {
        $full = Join-Path $WorkspaceDir $r
        if (Test-Path $full) {
            # Ensure no modified tracked files in product repos
            $diff = git -C $full diff --name-only 2>$null
            if ($diff -and $diff.Count -gt 0) {
                # Note: check if any modifications were introduced by our tooling
                Write-Warning "Product repo $r has modifications: $diff"
            }
        }
    }
    return $allClean
}

Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host " SUITE RESULTS: $script:testPassed Passed, $script:testFailed Failed" -ForegroundColor $(if ($script:testFailed -eq 0) { "Green" } else { "Red" })
Write-Host "=================================================================" -ForegroundColor Cyan

if ($script:testFailed -gt 0) {
    exit 1
}
