[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TestScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDirectory = Split-Path -Parent $TestScriptDirectory
$HelperPath = Join-Path $WorkspaceDirectory "agent-worktree.ps1"
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

function Get-RepositoryFingerprint([string]$RepositoryPath) {
    if (-not (Test-Path -LiteralPath $RepositoryPath)) { return "missing" }
    $head = ((@(& git -C $RepositoryPath rev-parse HEAD 2>$null)) -join ([Environment]::NewLine)).Trim()
    $status = @(
        & git -C $RepositoryPath status --porcelain=v1 --untracked-files=all 2>$null
    ) -join ([Environment]::NewLine)
    return ($head + "|" + $status)
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " RUNNING AGENT WORKTREE ISOLATION ACCEPTANCE SUITE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

$testId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("cyrene-wt-test-" + $testId)
$fixtureRepo = Join-Path $sandbox "mock-plugin-repo"
$fixtureRoot = Join-Path $sandbox "cwt"
$writerRole = "idea-spring-test"
$snapshotRole = "rider-media-snapshot-test"
$writerBranch = "chore/spring-boot-test-1"
$unknownPath = Join-Path $fixtureRoot "unregistered-test"
$businessRepositories = @(
    (Join-Path $WorkspaceDirectory "..\Cyrene-Platform"),
    (Join-Path $WorkspaceDirectory "..\Cyrene-Plugins-Official"),
    (Join-Path $WorkspaceDirectory "..\Services\Cyrene-Reactor"),
    (Join-Path $WorkspaceDirectory "..\Services\Cyrene-Yield"),
    (Join-Path $WorkspaceDirectory "..\Services\Cyrene-Exchange")
)
$businessBefore = @{}

try {
    New-Item -ItemType Directory -Path $fixtureRepo,$fixtureRoot -Force | Out-Null
    Invoke-FixtureGit $fixtureRepo @("init", "-b", "develop") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("config", "user.name", "Cyrene Test Agent") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("config", "user.email", "agent@cyrene.invalid") | Out-Null

    Set-Content -LiteralPath (Join-Path $fixtureRepo "README.md") -Value "# Mock Plugin Repo"
    Invoke-FixtureGit $fixtureRepo @("add", "README.md") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("commit", "-m", "test: initial fixture commit") | Out-Null
    $initialSha = Get-FixtureGitText $fixtureRepo @("rev-parse", "HEAD")

    Set-Content -LiteralPath (Join-Path $fixtureRepo "second.txt") -Value "second"
    Invoke-FixtureGit $fixtureRepo @("add", "second.txt") | Out-Null
    Invoke-FixtureGit $fixtureRepo @("commit", "-m", "test: second fixture commit") | Out-Null
    $baseSha = Get-FixtureGitText $fixtureRepo @("rev-parse", "HEAD")

    foreach ($businessRepository in $businessRepositories) {
        if (Test-Path -LiteralPath $businessRepository) {
            $businessBefore[(Normalize-PathValue $businessRepository)] = Get-RepositoryFingerprint $businessRepository
        }
    }

    Assert-Condition "task worktree creation resolves and records exact base" {
        $result = Invoke-Helper @(
            "create", "-Repo", $fixtureRepo, "-Branch", $writerBranch,
            "-Base", "develop", "-Role", $writerRole, "-Root", $fixtureRoot
        )
        if ($result.ExitCode -ne 0) { throw $result.Text }
        $writerPath = Join-Path $fixtureRoot $writerRole
        $head = Get-FixtureGitText $writerPath @("rev-parse", "HEAD")
        $branch = Get-FixtureGitText $writerPath @("branch", "--show-current")
        return ($head -eq $baseSha -and $branch -eq $writerBranch -and $result.Text -match [Regex]::Escape($baseSha))
    }

    Assert-Condition "exact-SHA snapshot is detached and marked" {
        $result = Invoke-Helper @(
            "snapshot", "-Repo", $fixtureRepo, "-Sha", $initialSha,
            "-Role", $snapshotRole, "-Root", $fixtureRoot
        )
        if ($result.ExitCode -ne 0) { throw $result.Text }
        $snapshotPath = Join-Path $fixtureRoot $snapshotRole
        $symbolicOutput = @(& git -C $snapshotPath symbolic-ref --quiet --short HEAD 2>$null)
        $symbolicExit = $LASTEXITCODE
        $marker = ConvertFrom-Json (Get-Content -LiteralPath (Join-Path $snapshotPath ".cyrene-snapshot.json") -Raw)
        return (
            (Get-FixtureGitText $snapshotPath @("rev-parse", "HEAD")) -eq $initialSha -and
            $symbolicExit -ne 0 -and
            [string]$marker.sha -eq $initialSha -and
            [string]$marker.mutability -eq "detached-exact-sha" -and
            $marker.filesystemReadOnly -eq $false
        )
    }

    Assert-Condition "status exposes registered agent ownership and modes" {
        $result = Invoke-Helper @("status", "-Root", $fixtureRoot)
        if ($result.ExitCode -ne 0) { throw $result.Text }
        return (
            $result.Text -match [Regex]::Escape($writerRole) -and
            $result.Text -match [Regex]::Escape($snapshotRole) -and
            $result.Text -match "writer" -and
            $result.Text -match "snapshot" -and
            $result.Text -match [Regex]::Escape($initialSha.Substring(0, 7))
        )
    }

    Assert-Condition "duplicate path and branch detection is fail-closed" {
        $idempotent = Invoke-Helper @(
            "create", "-Repo", $fixtureRepo, "-Branch", $writerBranch,
            "-Base", "develop", "-Role", $writerRole, "-Root", $fixtureRoot
        )
        if ($idempotent.ExitCode -ne 0 -or $idempotent.Text -notmatch "IDEMPOTENT") {
            throw "Expected idempotent create, got exit $($idempotent.ExitCode): $($idempotent.Text)"
        }
        $sameRole = Invoke-Helper @(
            "create", "-Repo", $fixtureRepo, "-Branch", "chore/conflicting-role",
            "-Base", "develop", "-Role", $writerRole, "-Root", $fixtureRoot
        )
        $sameBranch = Invoke-Helper @(
            "create", "-Repo", $fixtureRepo, "-Branch", $writerBranch,
            "-Base", "develop", "-Role", "another-role", "-Root", $fixtureRoot
        )
        $badBase = Invoke-Helper @(
            "create", "-Repo", $fixtureRepo, "-Branch", "chore/bad-base",
            "-Base", "refs/heads/does-not-exist", "-Role", "bad-base", "-Root", $fixtureRoot
        )
        return ($sameRole.ExitCode -ne 0 -and $sameBranch.ExitCode -ne 0 -and $badBase.ExitCode -ne 0)
    }

    Assert-Condition "short SHA input is refused for snapshots" {
        $result = Invoke-Helper @(
            "snapshot", "-Repo", $fixtureRepo, "-Sha", $initialSha.Substring(0, 7),
            "-Role", "short-sha", "-Root", $fixtureRoot
        )
        return ($result.ExitCode -ne 0 -and $result.Text -match "40-character")
    }

    Assert-Condition "dirty worktree cleanup is refused" {
        $writerPath = Join-Path $fixtureRoot $writerRole
        Set-Content -LiteralPath (Join-Path $writerPath "dirty.txt") -Value "uncommitted"
        $result = Invoke-Helper @("remove", "-Role", $writerRole, "-Root", $fixtureRoot)
        return ($result.ExitCode -ne 0 -and (Test-Path -LiteralPath $writerPath))
    }

    Assert-Condition "clean but unpushed writer cleanup is refused" {
        $writerPath = Join-Path $fixtureRoot $writerRole
        Invoke-FixtureGit $writerPath @("add", "dirty.txt") | Out-Null
        Invoke-FixtureGit $writerPath @("commit", "-m", "test: unpushed fixture work") | Out-Null
        $result = Invoke-Helper @("remove", "-Role", $writerRole, "-Root", $fixtureRoot)
        return ($result.ExitCode -ne 0 -and (Test-Path -LiteralPath $writerPath) -and $result.Text -match "unpushed|base")
    }

    Assert-Condition "unknown worktree cleanup is refused without confirmation" {
        Invoke-FixtureGit $fixtureRepo @(
            "worktree", "add", "-b", "chore/unregistered-test", $unknownPath, $baseSha
        ) | Out-Null
        $result = Invoke-Helper @("remove", "-Path", $unknownPath, "-Root", $fixtureRoot)
        return ($result.ExitCode -ne 0 -and (Test-Path -LiteralPath $unknownPath) -and $result.Text -match "unknown|unregistered|REFUSED")
    }

    Assert-Condition "explicit confirmation removes unknown disposable worktree safely" {
        $result = Invoke-Helper @("remove", "-Path", $unknownPath, "-Root", $fixtureRoot, "-Force")
        if ($result.ExitCode -ne 0) { throw $result.Text }
        $worktreeList = (Invoke-FixtureGit $fixtureRepo @("worktree", "list", "--porcelain")) -join ([Environment]::NewLine)
        return (-not (Test-Path -LiteralPath $unknownPath) -and $worktreeList -notmatch [Regex]::Escape($unknownPath))
    }

    Assert-Condition "clean snapshot removal clears Git metadata and registry" {
        $snapshotPath = Join-Path $fixtureRoot $snapshotRole
        $result = Invoke-Helper @("remove", "-Role", $snapshotRole, "-Root", $fixtureRoot)
        if ($result.ExitCode -ne 0) { throw $result.Text }
        $worktreeList = (Invoke-FixtureGit $fixtureRepo @("worktree", "list", "--porcelain")) -join ([Environment]::NewLine)
        $registry = Get-Content -LiteralPath (Join-Path $fixtureRoot ".registry.json") -Raw
        return (-not (Test-Path -LiteralPath $snapshotPath) -and $worktreeList -notmatch [Regex]::Escape($snapshotPath) -and $registry -notmatch $snapshotRole)
    }

    Assert-Condition "explicit confirmation removes unpushed writer" {
        $writerPath = Join-Path $fixtureRoot $writerRole
        $result = Invoke-Helper @("remove", "-Role", $writerRole, "-Root", $fixtureRoot, "-Force")
        if ($result.ExitCode -ne 0) { throw $result.Text }
        $worktreeList = (Invoke-FixtureGit $fixtureRepo @("worktree", "list", "--porcelain")) -join ([Environment]::NewLine)
        return (-not (Test-Path -LiteralPath $writerPath) -and $worktreeList -notmatch [Regex]::Escape($writerPath))
    }

    Assert-Condition "CYRENE_WORKTREE_ROOT override and short fallback are reported" {
        $oldRoot = $env:CYRENE_WORKTREE_ROOT
        try {
            $env:CYRENE_WORKTREE_ROOT = Join-Path $sandbox "env-root"
            $override = Invoke-Helper @("status")
            if ($override.ExitCode -ne 0 -or $override.Text -notmatch "env-root") {
                throw "Environment root was not reported: $($override.Text)"
            }
            Remove-Item Env:CYRENE_WORKTREE_ROOT -ErrorAction SilentlyContinue
            $fallback = Invoke-Helper @("status")
            return ($fallback.ExitCode -eq 0 -and $fallback.Text -match "Resolved worktree root: .*cwt")
        } finally {
            if ($null -eq $oldRoot) {
                Remove-Item Env:CYRENE_WORKTREE_ROOT -ErrorAction SilentlyContinue
            } else {
                $env:CYRENE_WORKTREE_ROOT = $oldRoot
            }
        }
    }

    Assert-Condition "business repository fingerprints are unchanged" {
        foreach ($businessRepository in $businessRepositories) {
            $key = Normalize-PathValue $businessRepository
            if ($businessBefore.ContainsKey($key)) {
                $after = Get-RepositoryFingerprint $businessRepository
                if ($after -ne $businessBefore[$key]) {
                    throw "Business repository changed during fixture validation: $businessRepository"
                }
            }
        }
        return $true
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
