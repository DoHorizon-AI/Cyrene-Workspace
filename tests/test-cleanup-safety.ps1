<#
.SYNOPSIS
    Deterministic acceptance tests for C:\cwt worktree ownership and cleanup safety.

.DESCRIPTION
    Validates all 11 worktree cleanup safety scenarios:
    1. MANAGED_WRITER_WORKTREE clean removal
    2. MANAGED_WRITER_WORKTREE dirty refusal (fail closed)
    3. MANAGED_WRITER_WORKTREE unpushed commits refusal (fail closed)
    4. MANAGED_WRITER_WORKTREE with -Force removal
    5. MANAGED_SNAPSHOT clean removal
    6. MANAGED_SNAPSHOT dirty refusal (fail closed)
    7. MANAGED_SNAPSHOT multi-role sharing (deregisters single role, preserves worktree)
    8. MANAGED_SNAPSHOT changed HEAD refusal (fail closed)
    9. MANAGED_SNAPSHOT missing/tampered marker refusal (fail closed)
    10. FOREIGN_OR_UNMANAGED_DIRECTORY preservation & removal refusal (fail closed)
    11. Missing worktree path on disk with stale registry cleanup
    12. agent-task.ps1 isolated state and READY_TO_EDIT clean invariant
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TestScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDirectory = Split-Path -Parent $TestScriptDirectory
$HelperPath = Join-Path $WorkspaceDirectory "agent-worktree.ps1"
$TaskHelperPath = Join-Path $WorkspaceDirectory "agent-task.ps1"

$Passed = 0
$Failed = 0

function Normalize-PathValue([string]$Value) {
    $full = [System.IO.Path]::GetFullPath($Value)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([char[]]@("\", "/"))
    }
    return $full
}

function Test-PathEqual([string]$Left, [string]$Right) {
    return [System.String]::Equals(
        (Normalize-PathValue $Left),
        (Normalize-PathValue $Right),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Invoke-FixtureGit {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )
    $output = @(& git -C $RepositoryPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $details = $output -join ([Environment]::NewLine)
        throw "Fixture git command failed: git -C '$RepositoryPath' $($Arguments -join " ") $details"
    }
    return @($output)
}

function Get-FixtureGitText([string]$RepositoryPath, [string[]]$Arguments) {
    return ((Invoke-FixtureGit $RepositoryPath $Arguments) -join ([Environment]::NewLine)).Trim()
}

function Invoke-Helper {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = @(& pwsh -NoProfile -File $HelperPath @Arguments 2>&1 | ForEach-Object { [string]$_ })
    return [PSCustomObject]@{
        ExitCode = $LASTEXITCODE
        Output = @($output)
        Text = ($output -join ([Environment]::NewLine))
    }
}

function Invoke-TaskHelper {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = @(& pwsh -NoProfile -File $TaskHelperPath @Arguments 2>&1 | ForEach-Object { [string]$_ })
    return [PSCustomObject]@{
        ExitCode = $LASTEXITCODE
        Output = @($output)
        Text = ($output -join ([Environment]::NewLine))
    }
}

function Assert-Condition([string]$Name, [scriptblock]$Condition) {
    Write-Host -NoNewline "  Testing $Name... "
    try {
        $result = & $Condition
        if ($result -eq $false) { throw "Condition returned false." }
        Write-Host "[PASS]" -ForegroundColor Green
        $script:Passed++
    } catch {
        Write-Host "[FAIL]" -ForegroundColor Red
        Write-Host "    $($_.Exception.Message)" -ForegroundColor Red
        $script:Failed++
    }
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " RUNNING C:\cwt WORKTREE OWNERSHIP & CLEANUP SAFETY SUITE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("cyrene-safety-test-" + $testId)
$fixtureRepo = Join-Path $sandbox "mock-safety-repo"
$fixtureRoot = Join-Path $sandbox "cwt"

try {
    New-Item -ItemType Directory -Path $fixtureRepo,$fixtureRoot -Force | Out-Null
    Invoke-FixtureGit $fixtureRepo @("init", "-b", "develop") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("config", "user.name", "Safety Test Agent") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("config", "user.email", "safety@cyrene.invalid") | Out-Null

    Set-Content -LiteralPath (Join-Path $fixtureRepo "README.md") -Value "# Mock Safety Repo"
    Invoke-FixtureGit $fixtureRepo @("add", "README.md") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("commit", "-m", "test: initial commit") | Out-Null
    $commit1 = Get-FixtureGitText $fixtureRepo @("rev-parse", "HEAD")

    Set-Content -LiteralPath (Join-Path $fixtureRepo "file2.txt") -Value "file2"
    Invoke-FixtureGit $fixtureRepo @("add", "file2.txt") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("commit", "-m", "test: second commit") | Out-Null
    $commit2 = Get-FixtureGitText $fixtureRepo @("rev-parse", "HEAD")

    # 1. MANAGED_WRITER_WORKTREE clean removal
    Assert-Condition "1. MANAGED_WRITER_WORKTREE clean removal succeeds" {
        $role = "writer-clean-test"
        $branch = "feat/writer-clean-test"
        $cRes = Invoke-Helper @("create", "-Repo", $fixtureRepo, "-Branch", $branch, "-Base", "develop", "-Role", $role, "-Root", $fixtureRoot)
        if ($cRes.ExitCode -ne 0) { throw $cRes.Text }
        $wtPath = Join-Path $fixtureRoot $role
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        if ($rRes.ExitCode -ne 0) { throw $rRes.Text }
        return (-not (Test-Path -LiteralPath $wtPath))
    }

    # 2. MANAGED_WRITER_WORKTREE dirty refusal (fail closed)
    Assert-Condition "2. MANAGED_WRITER_WORKTREE dirty removal is refused (fails closed)" {
        $role = "writer-dirty-test"
        $branch = "feat/writer-dirty-test"
        $cRes = Invoke-Helper @("create", "-Repo", $fixtureRepo, "-Branch", $branch, "-Base", "develop", "-Role", $role, "-Root", $fixtureRoot)
        if ($cRes.ExitCode -ne 0) { throw $cRes.Text }
        $wtPath = Join-Path $fixtureRoot $role
        Set-Content -LiteralPath (Join-Path $wtPath "dirty.txt") -Value "uncommitted modifications"
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        return ($rRes.ExitCode -ne 0 -and (Test-Path -LiteralPath $wtPath) -and $rRes.Text -match "dirty|REFUSED")
    }

    # 3. MANAGED_WRITER_WORKTREE unpushed commits refusal (fail closed)
    Assert-Condition "3. MANAGED_WRITER_WORKTREE unpushed commits removal is refused (fails closed)" {
        $role = "writer-dirty-test" # reuse existing from test 2
        $wtPath = Join-Path $fixtureRoot $role
        Invoke-FixtureGit $wtPath @("add", "dirty.txt") | Out-Null
        Invoke-FixtureGit $wtPath @("commit", "-m", "test: committed but unpushed") | Out-Null
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        return ($rRes.ExitCode -ne 0 -and (Test-Path -LiteralPath $wtPath) -and $rRes.Text -match "unpushed|base|REFUSED")
    }

    # 4. MANAGED_WRITER_WORKTREE with -Force removal
    Assert-Condition "4. MANAGED_WRITER_WORKTREE with -Force removes cleanly" {
        $role = "writer-dirty-test"
        $wtPath = Join-Path $fixtureRoot $role
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot, "-Force")
        if ($rRes.ExitCode -ne 0) { throw $rRes.Text }
        return (-not (Test-Path -LiteralPath $wtPath))
    }

    # 5. MANAGED_SNAPSHOT clean removal
    Assert-Condition "5. MANAGED_SNAPSHOT clean removal succeeds" {
        $role = "snapshot-clean-test"
        $cRes = Invoke-Helper @("snapshot", "-Repo", $fixtureRepo, "-Sha", $commit1, "-Role", $role, "-Root", $fixtureRoot)
        if ($cRes.ExitCode -ne 0) { throw $cRes.Text }
        $wtPath = Join-Path $fixtureRoot $role
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        if ($rRes.ExitCode -ne 0) { throw $rRes.Text }
        return (-not (Test-Path -LiteralPath $wtPath))
    }

    # 6. MANAGED_SNAPSHOT dirty refusal (fails closed)
    Assert-Condition "6. MANAGED_SNAPSHOT dirty removal is refused (fails closed)" {
        $role = "snapshot-dirty-test"
        $cRes = Invoke-Helper @("snapshot", "-Repo", $fixtureRepo, "-Sha", $commit1, "-Role", $role, "-Root", $fixtureRoot)
        if ($cRes.ExitCode -ne 0) { throw $cRes.Text }
        $wtPath = Join-Path $fixtureRoot $role
        Set-Content -LiteralPath (Join-Path $wtPath "dirty.txt") -Value "uncommitted in snapshot"
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        $refused = ($rRes.ExitCode -ne 0 -and (Test-Path -LiteralPath $wtPath))
        # clean up with Force
        Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot, "-Force") | Out-Null
        return $refused
    }

    # 7. MANAGED_SNAPSHOT multi-role sharing
    Assert-Condition "7. MANAGED_SNAPSHOT shared by multiple roles deregisters role only and preserves shared worktree" {
        $roleA = "snapshot-role-a"
        $roleB = "snapshot-role-b"
        $cResA = Invoke-Helper @("snapshot", "-Repo", $fixtureRepo, "-Sha", $commit1, "-Role", $roleA, "-Root", $fixtureRoot)
        $cResB = Invoke-Helper @("snapshot", "-Repo", $fixtureRepo, "-Sha", $commit1, "-Role", $roleB, "-Root", $fixtureRoot)
        if ($cResA.ExitCode -ne 0 -or $cResB.ExitCode -ne 0) { throw "$($cResA.Text) ; $($cResB.Text)" }
        $wtPath = Join-Path $fixtureRoot $roleA
        
        # Remove role A
        $rResA = Invoke-Helper @("remove", "-Role", $roleA, "-Root", $fixtureRoot)
        if ($rResA.ExitCode -ne 0) { throw $rResA.Text }
        
        # Path must still exist because role B is attached
        $pathPreserved = (Test-Path -LiteralPath $wtPath)
        
        # Remove role B
        $rResB = Invoke-Helper @("remove", "-Role", $roleB, "-Root", $fixtureRoot)
        if ($rResB.ExitCode -ne 0) { throw $rResB.Text }
        
        $pathRemoved = (-not (Test-Path -LiteralPath $wtPath))
        return ($pathPreserved -and $pathRemoved)
    }

    # 8. MANAGED_SNAPSHOT changed HEAD refusal
    Assert-Condition "8. MANAGED_SNAPSHOT changed HEAD removal is refused (fails closed)" {
        $role = "snapshot-head-change"
        $cRes = Invoke-Helper @("snapshot", "-Repo", $fixtureRepo, "-Sha", $commit1, "-Role", $role, "-Root", $fixtureRoot)
        if ($cRes.ExitCode -ne 0) { throw $cRes.Text }
        $wtPath = Join-Path $fixtureRoot $role
        
        # Switch checkout to commit2 without updating marker
        Invoke-FixtureGit $wtPath @("checkout", $commit2) | Out-Null
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        $refused = ($rRes.ExitCode -ne 0 -and (Test-Path -LiteralPath $wtPath) -and $rRes.Text -match "exact SHA|REFUSED")
        Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot, "-Force") | Out-Null
        return $refused
    }

    # 9. MANAGED_SNAPSHOT missing/tampered marker refusal
    Assert-Condition "9. MANAGED_SNAPSHOT missing marker removal is refused (fails closed)" {
        $role = "snapshot-marker-test"
        $cRes = Invoke-Helper @("snapshot", "-Repo", $fixtureRepo, "-Sha", $commit1, "-Role", $role, "-Root", $fixtureRoot)
        if ($cRes.ExitCode -ne 0) { throw $cRes.Text }
        $wtPath = Join-Path $fixtureRoot $role
        
        # Delete marker
        Remove-Item -LiteralPath (Join-Path $wtPath ".cyrene-snapshot.json") -Force
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        $refused = ($rRes.ExitCode -ne 0 -and (Test-Path -LiteralPath $wtPath) -and $rRes.Text -match "marker|REFUSED")
        Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot, "-Force") | Out-Null
        return $refused
    }

    # 10. FOREIGN_OR_UNMANAGED_DIRECTORY preservation & removal refusal
    Assert-Condition "10. FOREIGN_OR_UNMANAGED_DIRECTORY is preserved and automated removal is strictly refused" {
        $foreignDir = Join-Path $fixtureRoot "codebuddy-foreign-folder"
        New-Item -ItemType Directory -Path $foreignDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $foreignDir "critical_work.txt") -Value "Do not delete me"
        
        # Check status detects as foreign/unmanaged
        $statusRes = Invoke-Helper @("status", "-Root", $fixtureRoot)
        $statusOk = ($statusRes.Text -match "FOREIGN_OR_UNMANAGED_DIRECTORY" -and $statusRes.Text -match "unmanaged / needs_review")
        
        # Try to remove foreign directory
        $rRes = Invoke-Helper @("remove", "-Path", $foreignDir, "-Root", $fixtureRoot)
        $refused = ($rRes.ExitCode -ne 0 -and (Test-Path -LiteralPath (Join-Path $foreignDir "critical_work.txt")))
        
        # Clean up manually
        Remove-Item -LiteralPath $foreignDir -Recurse -Force
        return ($statusOk -and $refused)
    }

    # 11. Missing worktree path on disk with stale registry cleanup
    Assert-Condition "11. Missing worktree path on disk cleans up stale registry entry safely" {
        $role = "stale-reg-test"
        $branch = "feat/stale-reg-test"
        $cRes = Invoke-Helper @("create", "-Repo", $fixtureRepo, "-Branch", $branch, "-Base", "develop", "-Role", $role, "-Root", $fixtureRoot)
        $wtPath = Join-Path $fixtureRoot $role
        
        # Simulate path vanishing from disk without unregistering
        Remove-Item -LiteralPath $wtPath -Recurse -Force
        
        $rRes = Invoke-Helper @("remove", "-Role", $role, "-Root", $fixtureRoot)
        if ($rRes.ExitCode -ne 0) { throw $rRes.Text }
        return ($rRes.Text -match "Removed stale local metadata")
    }

    # 12. agent-task.ps1 isolated state and READY_TO_EDIT clean invariant
    Assert-Condition "12. agent-task.ps1 isolates state in .state and enforces 100% clean READY_TO_EDIT" {
        $taskRole = "task-clean-test"
        $taskBranch = "feat/task-clean-test"
        $tStart = Invoke-TaskHelper @("start", "-Repo", $fixtureRepo, "-Branch", $taskBranch, "-Base", "develop", "-Role", $taskRole, "-Root", $fixtureRoot, "-SkipIDE")
        if ($tStart.ExitCode -ne 0) { throw $tStart.Text }
        
        $targetWt = Join-Path $fixtureRoot $taskRole
        $stateLedger = Join-Path $fixtureRoot (Join-Path ".state" (Join-Path (Split-Path $fixtureRepo -Leaf) (Join-Path $taskRole "WORKSPACE.md")))
        
        # Working tree must have 0 untracked files
        $statusPorcelain = @(& git -C $targetWt status --porcelain=v1 --untracked-files=all)
        $isClean = ($statusPorcelain.Count -eq 0)
        $emittedReady = ($tStart.Text -match "READY_TO_EDIT")
        $ledgerExists = (Test-Path -LiteralPath $stateLedger)
        
        # Cleanup
        Invoke-TaskHelper @("stop", "-Role", $taskRole, "-Root", $fixtureRoot, "-Force") | Out-Null
        return ($isClean -and $emittedReady -and $ledgerExists)
    }

} finally {
    if (Test-Path -LiteralPath $fixtureRepo) {
        $remaining = @(& git -C $fixtureRepo worktree list --porcelain 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $paths = @()
            foreach ($line in $remaining) {
                if ([string]$line -match '^worktree\s+(.+)$') {
                    $candidate = $matches[1].Trim()
                    if (-not (Test-PathEqual $candidate $fixtureRepo)) {
                        $paths += $candidate
                    }
                }
            }
            foreach ($candidate in $paths) {
                & git -C $fixtureRepo worktree remove --force $candidate 2>$null | Out-Null
            }
        }
    }
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " SUITE RESULTS: $Passed Passed, $Failed Failed" -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
Write-Host "=================================================================" -ForegroundColor Cyan
if ($Failed -gt 0) {
    exit 1
}
