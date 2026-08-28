<#
.SYNOPSIS
    Parallel Agent Worktree Isolation Helper for Cyrene Workspace.
.DESCRIPTION
    Safely creates, manages, snapshots, and removes dedicated Git worktrees for parallel
    Cyrene coding agents. Prevents agents from treating another agent's mutable working tree
    as an integration baseline.
.EXAMPLE
    .\agent-worktree.ps1 create -Repo plugins -Branch chore/spring-boot-4-1-1 -Base origin/develop -Role idea-spring
    .\agent-worktree.ps1 snapshot -Repo Cyrene-Platform -Sha 4449fe770ece8bf2ef27271b109dcd8b715f869a -Role rider-media-platform
    .\agent-worktree.ps1 status
    .\agent-worktree.ps1 remove -Role idea-spring
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("create", "snapshot", "status", "list", "remove", "cleanup")]
    [string]$Action,

    [Parameter(Position = 1)]
    [Alias("Repository")]
    [string]$Repo,

    [Parameter()]
    [string]$Branch,

    [Parameter()]
    [string]$Base,

    [Parameter()]
    [string]$Sha,

    [Parameter()]
    [string]$Role,

    [Parameter()]
    [Alias("WorktreePath")]
    [string]$Path,

    [Parameter()]
    [Alias("WorktreeRoot")]
    [string]$Root,

    [Parameter()]
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- 1. Topology & Repositories Resolution ---

function Get-WorkspaceRepositories {
    $manifestPath = Join-Path $ScriptDir "repositories.yaml"
    if (-not (Test-Path $manifestPath)) {
        throw "repositories.yaml not found at '$manifestPath'"
    }

    $lines = Get-Content $manifestPath
    $repos = @()
    $current = $null

    foreach ($line in $lines) {
        if ($line -match '^\s*-\s*name:\s*"([^"]+)"') {
            if ($current) { $repos += [PSCustomObject]$current }
            $current = @{
                Name = $matches[1]
                Path = ""
                CanonicalRemote = ""
                IntegrationBranch = "develop"
            }
        } elseif ($current) {
            if ($line -match '^\s*path:\s*"([^"]+)"') {
                $current.Path = $matches[1]
            } elseif ($line -match '^\s*canonical_remote:\s*"([^"]+)"') {
                $current.CanonicalRemote = $matches[1]
            } elseif ($line -match '^\s*integration_branch:\s*"([^"]+)"') {
                $current.IntegrationBranch = $matches[1]
            }
        }
    }
    if ($current) { $repos += [PSCustomObject]$current }

    foreach ($r in $repos) {
        $r | Add-Member -NotePropertyName FullPath -NotePropertyValue (Join-Path $ScriptDir $r.Path) -Force
    }

    return $repos
}

function Resolve-Repository([string]$repoQuery) {
    if ([string]::IsNullOrWhiteSpace($repoQuery)) {
        throw "Repository (-Repo) must be specified."
    }

    $allRepos = Get-WorkspaceRepositories

    # 1. Exact Name match
    $match = $allRepos | Where-Object { $_.Name -eq $repoQuery }
    if ($match) { return $match[0] }

    # 2. Case-insensitive Name match
    $match = $allRepos | Where-Object { $_.Name.ToLower() -eq $repoQuery.ToLower() }
    if ($match) { return $match[0] }

    # 3. Path basename match (e.g. "plugins", "Cyrene-Platform", "cyrene-astrbot-rev")
    $match = $allRepos | Where-Object { (Split-Path $_.Path -Leaf).ToLower() -eq $repoQuery.ToLower() }
    if ($match) { return $match[0] }

    # 4. Common Stem / Alias matching
    $aliasMap = @{
        "platform" = "Cyrene-Platform"
        "plugins" = "Cyrene-Plugins"
        "astrbot" = "cyrene-astrbot-rev"
        "astrbot-rev" = "cyrene-astrbot-rev"
        "internal" = "cyrene-dh-system-internal"
        "dh-system" = "cyrene-dh-system-internal"
        "dh-system-internal" = "cyrene-dh-system-internal"
        "reactor" = "cyrene-reactor"
        "yield" = "Cyrene-Yield"
        "exchange" = "cyrene-exchange"
        "catalyst" = "cyrene-catalyst"
        "echo" = "cyrene-echo"
        "navigator" = "cyrene-navigator"
    }

    $cleanQuery = $repoQuery.ToLower().Trim()
    if ($aliasMap.ContainsKey($cleanQuery)) {
        $aliasedName = $aliasMap[$cleanQuery]
        $match = $allRepos | Where-Object { $_.Name -eq $aliasedName }
        if ($match) { return $match[0] }
    }

    # 5. Direct path if valid git repo
    if (Test-Path $repoQuery) {
        $resolved = (Resolve-Path $repoQuery).Path
        if (Test-Path (Join-Path $resolved ".git")) {
            return [PSCustomObject]@{
                Name = Split-Path $resolved -Leaf
                Path = $repoQuery
                FullPath = $resolved
                CanonicalRemote = ""
                IntegrationBranch = "develop"
            }
        }
    }

    $availableNames = ($allRepos | ForEach-Object { "$($_.Name) ($((Split-Path $_.Path -Leaf)))" }) -join ", "
    throw "Repository '$repoQuery' not found in repositories.yaml. Available: $availableNames"
}

# --- 2. Short Windows Path Resolution ---

function Get-WorktreeRoot([string]$customRoot) {
    if (-not [string]::IsNullOrWhiteSpace($customRoot)) {
        if (-not (Test-Path $customRoot)) {
            New-Item -ItemType Directory -Path $customRoot -Force | Out-Null
        }
        return (Resolve-Path $customRoot).Path
    }

    if (-not [string]::IsNullOrWhiteSpace($env:CYRENE_WORKTREE_ROOT)) {
        if (-not (Test-Path $env:CYRENE_WORKTREE_ROOT)) {
            New-Item -ItemType Directory -Path $env:CYRENE_WORKTREE_ROOT -Force | Out-Null
        }
        return (Resolve-Path $env:CYRENE_WORKTREE_ROOT).Path
    }

    # Candidate 1: Short root on system drive, e.g. C:\cwt
    if ($IsWindows -or ($PSVersionTable.PSVersion.Major -le 5)) {
        $sysDrive = if ($env:SystemDrive) { $env:SystemDrive } else { "C:" }
        $shortCandidate = "$sysDrive\cwt"
        try {
            if (-not (Test-Path $shortCandidate)) {
                New-Item -ItemType Directory -Path $shortCandidate -Force -ErrorAction Stop | Out-Null
            }
            # Test write access
            $testFile = Join-Path $shortCandidate ".write-test-$([Guid]::NewGuid().ToString('N'))"
            Set-Content -Path $testFile -Value "test" -ErrorAction Stop
            Remove-Item -Path $testFile -Force -ErrorAction SilentlyContinue
            return (Resolve-Path $shortCandidate).Path
        } catch {
            # Fall through to temp if system drive root is not writable
        }

        # Candidate 2: Short temp directory
        $tempCandidate = Join-Path $env:TEMP "cwt"
        if (-not (Test-Path $tempCandidate)) {
            New-Item -ItemType Directory -Path $tempCandidate -Force | Out-Null
        }
        return (Resolve-Path $tempCandidate).Path
    } else {
        # Linux / macOS short temp root
        $nixCandidate = "/tmp/cwt"
        if (-not (Test-Path $nixCandidate)) {
            New-Item -ItemType Directory -Path $nixCandidate -Force | Out-Null
        }
        return (Resolve-Path $nixCandidate).Path
    }
}

# --- 3. Local Metadata Tracking ---

function Get-RegistryPath([string]$worktreeRoot) {
    return Join-Path $worktreeRoot ".registry.json"
}

function Get-WorktreeRegistry([string]$worktreeRoot) {
    $regPath = Get-RegistryPath $worktreeRoot
    if (Test-Path $regPath) {
        try {
            $content = Get-Content $regPath -Raw
            if (-not [string]::IsNullOrWhiteSpace($content)) {
                return (ConvertFrom-Json $content)
            }
        } catch {
            Write-Warning "Failed to parse registry at '$regPath': $($_.Exception.Message)"
        }
    }
    return @()
}

function Save-WorktreeRegistry([string]$worktreeRoot, $entries) {
    $regPath = Get-RegistryPath $worktreeRoot
    $json = ConvertTo-Json -InputObject @($entries) -Depth 10
    Set-Content -Path $regPath -Value $json -Force
}

function Add-RegistryEntry([string]$worktreeRoot, [hashtable]$entry) {
    $entries = @(Get-WorktreeRegistry $worktreeRoot)
    $filtered = $entries | Where-Object { $_.path -ne $entry.path -and $_.role -ne $entry.role }
    $obj = [PSCustomObject]$entry
    $updated = @($filtered) + @($obj)
    Save-WorktreeRegistry $worktreeRoot $updated
}

function Remove-RegistryEntry([string]$worktreeRoot, [string]$targetPath, [string]$targetRole) {
    $entries = @(Get-WorktreeRegistry $worktreeRoot)
    $filtered = $entries | Where-Object {
        ($targetPath -and $_.path -ne $targetPath) -or
        ($targetRole -and $_.role -ne $targetRole)
    }
    Save-WorktreeRegistry $worktreeRoot $filtered
}

# --- 4. Worktree Operations ---

function Invoke-CreateTaskWorktree {
    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Parameter -Repo is required for 'create'."
    }
    if ([string]::IsNullOrWhiteSpace($Branch)) {
        throw "Parameter -Branch is required for 'create'."
    }
    if ([string]::IsNullOrWhiteSpace($Role)) {
        throw "Parameter -Role is required for 'create' (e.g. idea-spring, rider-astrbot)."
    }

    $repoObj = Resolve-Repository $Repo
    if (-not (Test-Path $repoObj.FullPath)) {
        throw "Repository directory not found at '$($repoObj.FullPath)'. Run .\bootstrap.ps1 first."
    }

    $worktreeRoot = Get-WorktreeRoot $Root
    Write-Host "Resolved worktree root: $worktreeRoot" -ForegroundColor DarkGray

    $targetPath = if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $Path
    } else {
        Join-Path $worktreeRoot $Role
    }

    Write-Host "`n=== CREATE TASK WORKTREE ===" -ForegroundColor Cyan
    Write-Host "  Role:        $Role" -ForegroundColor White
    Write-Host "  Repository:  $($repoObj.Name) ($($repoObj.FullPath))" -ForegroundColor White
    Write-Host "  Branch:      $Branch" -ForegroundColor White
    Write-Host "  Destination: $targetPath" -ForegroundColor White

    # Check for existing worktree at destination
    if (Test-Path $targetPath) {
        $gitFile = Join-Path $targetPath ".git"
        if (Test-Path $gitFile) {
            $currentBranch = (git -C $targetPath rev-parse --abbrev-ref HEAD 2>$null).Trim()
            $registry = Get-WorktreeRegistry $worktreeRoot
            $existingEntry = $registry | Where-Object { $_.path -eq $targetPath }

            if ($currentBranch -eq $Branch -and $existingEntry -and $existingEntry.role -eq $Role) {
                Write-Host "  [IDEMPOTENT] Worktree already exists for role '$Role' on branch '$Branch' at $targetPath." -ForegroundColor Green
                return
            } else {
                throw "Worktree path '$targetPath' already exists with branch '$currentBranch'. Refusing to overwrite another worktree."
            }
        } else {
            throw "Destination path '$targetPath' already exists and is not a git worktree. Aborting to prevent data loss."
        }
    }

    # Check if branch is checked out elsewhere in this repo
    $existingWorktrees = git -C $repoObj.FullPath worktree list --porcelain 2>&1
    $worktreeText = $existingWorktrees -join "`n"
    if ($worktreeText -match "branch refs/heads/$([regex]::Escape($Branch))") {
        throw "Branch '$Branch' is already checked out in another worktree of repository '$($repoObj.Name)'."
    }

    # Fetch safely
    $remotes = (git -C $repoObj.FullPath remote 2>$null)
    if ($remotes -contains "origin") {
        Write-Host "  [FETCH] Fetching origin safely..." -ForegroundColor DarkGray
        git -C $repoObj.FullPath fetch origin 2>$null | Out-Null
    }

    # Resolve exact base SHA
    $baseRef = if (-not [string]::IsNullOrWhiteSpace($Base)) {
        $Base
    } elseif ($remotes -contains "origin") {
        "origin/$($repoObj.IntegrationBranch)"
    } else {
        $repoObj.IntegrationBranch
    }

    $resolvedBaseSha = (git -C $repoObj.FullPath rev-parse --verify "$baseRef^{commit}" 2>$null)
    if (-not $resolvedBaseSha) {
        # Fallback to local integration branch, main, or HEAD
        $fallbacks = @($repoObj.IntegrationBranch, "origin/main", "main", "origin/master", "master", "HEAD")
        foreach ($fb in $fallbacks) {
            $resolvedBaseSha = (git -C $repoObj.FullPath rev-parse --verify "$fb^{commit}" 2>$null)
            if ($resolvedBaseSha) {
                $baseRef = $fb
                break
            }
        }
    }

    if (-not $resolvedBaseSha) {
        throw "Failed to resolve base ref '$baseRef' in repository '$($repoObj.Name)'."
    }
    $resolvedBaseSha = $resolvedBaseSha.Trim()
    Write-Host "  [BASE SHA] Resolved $baseRef -> $resolvedBaseSha" -ForegroundColor Green

    # Check if branch already exists locally
    $branchExists = (git -C $repoObj.FullPath rev-parse --verify "refs/heads/$Branch" 2>$null)

    if ($branchExists) {
        Write-Host "  [ATTACH] Attaching to existing local branch '$Branch'..." -ForegroundColor Yellow
        $addOutput = git -C $repoObj.FullPath worktree add "$targetPath" "$Branch" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "git worktree add failed: $addOutput"
        }
    } else {
        Write-Host "  [CREATE] Creating new branch '$Branch' from $resolvedBaseSha..." -ForegroundColor Green
        $addOutput = git -C $repoObj.FullPath worktree add -b "$Branch" "$targetPath" "$resolvedBaseSha" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "git worktree add failed: $addOutput"
        }
    }

    # Record local metadata
    $entry = @{
        role = $Role
        repo = $repoObj.Name
        repoPath = $repoObj.Path
        path = (Resolve-Path $targetPath).Path
        mode = "writer"
        branch = $Branch
        baseRef = $baseRef
        baseSha = $resolvedBaseSha
        sha = (git -C $targetPath rev-parse HEAD).Trim()
        createdAt = ([DateTime]::UtcNow.ToString("o"))
    }
    Add-RegistryEntry $worktreeRoot $entry

    Write-Host "  [SUCCESS] Task worktree created successfully at $targetPath" -ForegroundColor Green
    Write-Host "  Mode: WRITER (mutable task branch for Agent '$Role')" -ForegroundColor Green
}

function Invoke-CreateSnapshotWorktree {
    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Parameter -Repo is required for 'snapshot'."
    }
    if ([string]::IsNullOrWhiteSpace($Sha)) {
        throw "Parameter -Sha is required for 'snapshot'."
    }
    if ([string]::IsNullOrWhiteSpace($Role)) {
        throw "Parameter -Role is required for 'snapshot' (e.g. rider-media-platform)."
    }

    $repoObj = Resolve-Repository $Repo
    if (-not (Test-Path $repoObj.FullPath)) {
        throw "Repository directory not found at '$($repoObj.FullPath)'. Run .\bootstrap.ps1 first."
    }

    $worktreeRoot = Get-WorktreeRoot $Root
    Write-Host "Resolved worktree root: $worktreeRoot" -ForegroundColor DarkGray

    $targetPath = if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $Path
    } else {
        Join-Path $worktreeRoot $Role
    }

    Write-Host "`n=== CREATE IMMUTABLE SNAPSHOT WORKTREE ===" -ForegroundColor Cyan
    Write-Host "  Role:        $Role" -ForegroundColor White
    Write-Host "  Repository:  $($repoObj.Name) ($($repoObj.FullPath))" -ForegroundColor White
    Write-Host "  Target SHA:  $Sha" -ForegroundColor White
    Write-Host "  Destination: $targetPath" -ForegroundColor White

    # Resolve exact SHA
    $resolvedSha = (git -C $repoObj.FullPath rev-parse --verify "$Sha^{commit}" 2>$null)
    if (-not $resolvedSha) {
        Write-Host "  [FETCH] SHA not found locally; attempting fetch from origin..." -ForegroundColor DarkGray
        git -C $repoObj.FullPath fetch origin $Sha 2>$null | Out-Null
        $resolvedSha = (git -C $repoObj.FullPath rev-parse --verify "$Sha^{commit}" 2>$null)
    }

    if (-not $resolvedSha) {
        throw "Failed to resolve commit SHA '$Sha' in repository '$($repoObj.Name)'."
    }
    $resolvedSha = $resolvedSha.Trim()
    Write-Host "  [EXACT SHA] Verified exact SHA -> $resolvedSha" -ForegroundColor Green

    # Check for existing worktree at destination
    if (Test-Path $targetPath) {
        $gitFile = Join-Path $targetPath ".git"
        if (Test-Path $gitFile) {
            $currentSha = (git -C $targetPath rev-parse HEAD 2>$null).Trim()
            $registry = Get-WorktreeRegistry $worktreeRoot
            $existingEntry = $registry | Where-Object { $_.path -eq $targetPath }

            if ($currentSha -eq $resolvedSha -and $existingEntry -and $existingEntry.role -eq $Role) {
                Write-Host "  [IDEMPOTENT] Snapshot worktree already exists for role '$Role' at exact SHA $resolvedSha." -ForegroundColor Green
                return
            } else {
                throw "Worktree path '$targetPath' already exists with SHA '$currentSha'. Refusing to overwrite."
            }
        } else {
            throw "Destination path '$targetPath' already exists and is not a git worktree. Aborting."
        }
    }

    # Create detached worktree
    Write-Host "  [CREATE] Creating detached exact-SHA worktree..." -ForegroundColor Green
    $addOutput = git -C $repoObj.FullPath worktree add --detach "$targetPath" "$resolvedSha" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git worktree add --detach failed: $addOutput"
    }

    # Mark clearly as integration snapshot
    $snapshotMeta = @{
        type = "CYRENE_INTEGRATION_SNAPSHOT"
        role = $Role
        repo = $repoObj.Name
        sha = $resolvedSha
        createdAt = ([DateTime]::UtcNow.ToString("o"))
        immutableSnapshot = $true
        warning = "DO NOT MUTATE: This worktree is an immutable integration snapshot for cross-Agent consumption."
    }
    $metaFile = Join-Path $targetPath ".cyrene-snapshot.json"
    Set-Content -Path $metaFile -Value (ConvertTo-Json -InputObject $snapshotMeta -Depth 5) -Force

    # Record local metadata
    $entry = @{
        role = $Role
        repo = $repoObj.Name
        repoPath = $repoObj.Path
        path = (Resolve-Path $targetPath).Path
        mode = "snapshot"
        branch = "(detached HEAD)"
        baseRef = ""
        baseSha = ""
        sha = $resolvedSha
        createdAt = ([DateTime]::UtcNow.ToString("o"))
    }
    Add-RegistryEntry $worktreeRoot $entry

    Write-Host "  [SUCCESS] Immutable snapshot worktree created at $targetPath" -ForegroundColor Green
    Write-Host "  Mode: SNAPSHOT (detached HEAD at exact SHA $resolvedSha)" -ForegroundColor Green
}

function Invoke-WorktreeStatus {
    $worktreeRoot = Get-WorktreeRoot $Root
    Write-Host "Resolved worktree root: $worktreeRoot" -ForegroundColor DarkGray
    $registry = Get-WorktreeRegistry $worktreeRoot

    Write-Host "`n====================================================================================================" -ForegroundColor Cyan
    Write-Host " CYRENE PARALLEL AGENT WORKTREE STATUS" -ForegroundColor Cyan
    Write-Host "====================================================================================================" -ForegroundColor Cyan

    $rows = @()

    # Add registered entries
    foreach ($entry in $registry) {
        $refDisplay = if ($entry.mode -eq "snapshot") {
            if ($entry.sha.Length -ge 7) { $entry.sha.Substring(0, 7) } else { $entry.sha }
        } else {
            $entry.branch
        }

        $exists = Test-Path $entry.path
        $statusNote = if ($exists) { "" } else { " (missing path)" }

        $rows += [PSCustomObject]@{
            ROLE    = $entry.role
            REPO    = $entry.repo
            MODE    = $entry.mode
            "REF/SHA" = "$refDisplay$statusNote"
            PATH    = $entry.path
            CREATED = if ($entry.createdAt) { $entry.createdAt } else { "N/A" }
        }
    }

    # Discover any unregistered git worktrees in canonical repositories
    try {
        $allRepos = Get-WorkspaceRepositories
        foreach ($repo in $allRepos) {
            if (Test-Path $repo.FullPath) {
                $wtLines = git -C $repo.FullPath worktree list --porcelain 2>$null
                $currentPath = ""
                $currentHead = ""
                $currentBranch = ""
                $isDetached = $false

                foreach ($line in $wtLines) {
                    if ($line -match '^worktree\s+(.+)$') {
                        $currentPath = $matches[1].Trim()
                        $currentHead = ""
                        $currentBranch = ""
                        $isDetached = $false
                    } elseif ($line -match '^HEAD\s+([0-9a-fA-F]+)') {
                        $currentHead = $matches[1].Trim()
                    } elseif ($line -match '^branch\s+refs/heads/(.+)$') {
                        $currentBranch = $matches[1].Trim()
                    } elseif ($line -match '^detached') {
                        $isDetached = $true
                    } elseif ([string]::IsNullOrWhiteSpace($line) -and $currentPath) {
                        # Ignore canonical repo primary worktree
                        $normCurrent = [System.IO.Path]::GetFullPath($currentPath).TrimEnd('\', '/')
                        $normRepo = [System.IO.Path]::GetFullPath($repo.FullPath).TrimEnd('\', '/')
                        if ($normCurrent -ne $normRepo) {
                            $alreadyTracked = $registry | Where-Object {
                                [System.IO.Path]::GetFullPath($_.path).TrimEnd('\', '/') -eq $normCurrent
                            }
                            if (-not $alreadyTracked) {
                                $ref = if ($isDetached) {
                                    if ($currentHead.Length -ge 7) { $currentHead.Substring(0, 7) } else { $currentHead }
                                } else {
                                    $currentBranch
                                }
                                $rows += [PSCustomObject]@{
                                    ROLE    = "(unregistered)"
                                    REPO    = $repo.Name
                                    MODE    = if ($isDetached) { "snapshot" } else { "writer" }
                                    "REF/SHA" = $ref
                                    PATH    = $currentPath
                                    CREATED = "discovered"
                                }
                            }
                        }
                        $currentPath = ""
                    }
                }
            }
        }
    } catch {
        # Non-fatal error scanning
    }

    if ($rows.Count -eq 0) {
        Write-Host "No active agent worktrees found." -ForegroundColor Gray
    } else {
        $fmt = "{0,-22} {1,-18} {2,-10} {3,-40} {4}"
        Write-Host ($fmt -f "ROLE", "REPO", "MODE", "REF/SHA", "PATH") -ForegroundColor Yellow
        Write-Host ($fmt -f ("-" * 22), ("-" * 18), ("-" * 10), ("-" * 40), ("-" * 35)) -ForegroundColor DarkGray
        foreach ($r in $rows) {
            $color = if ($r.MODE -eq "snapshot") { "Cyan" } else { "Green" }
            Write-Host ($fmt -f $r.ROLE, $r.REPO, $r.MODE, $r."REF/SHA", $r.PATH) -ForegroundColor $color
        }
    }
    Write-Host "====================================================================================================`n" -ForegroundColor Cyan
}

function Invoke-RemoveWorktree {
    if ([string]::IsNullOrWhiteSpace($Role) -and [string]::IsNullOrWhiteSpace($Path)) {
        throw "Either -Role or -Path must be specified for 'remove'/'cleanup'."
    }

    $worktreeRoot = Get-WorktreeRoot $Root
    $registry = Get-WorktreeRegistry $worktreeRoot

    $targetEntry = $null
    if (-not [string]::IsNullOrWhiteSpace($Role)) {
        $targetEntry = $registry | Where-Object { $_.role -eq $Role } | Select-Object -First 1
    }
    if (-not $targetEntry -and -not [string]::IsNullOrWhiteSpace($Path)) {
        $normPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
        $targetEntry = $registry | Where-Object {
            [System.IO.Path]::GetFullPath($_.path).TrimEnd('\', '/') -eq $normPath
        } | Select-Object -First 1
    }

    $targetPath = if ($targetEntry) {
        $targetEntry.path
    } elseif (-not [string]::IsNullOrWhiteSpace($Path)) {
        $Path
    } else {
        Join-Path $worktreeRoot $Role
    }

    Write-Host "`n=== REMOVE AGENT WORKTREE ===" -ForegroundColor Cyan
    Write-Host "  Target Path: $targetPath" -ForegroundColor White

    if (-not (Test-Path $targetPath)) {
        Write-Warning "Worktree directory does not exist at '$targetPath'."
        if ($targetEntry) {
            Remove-RegistryEntry $worktreeRoot $targetEntry.path $targetEntry.role
            Write-Host "Cleaned up orphaned registry entry for role '$($targetEntry.role)'." -ForegroundColor Yellow
        }
        return
    }

    # Verify target is a git worktree
    $gitFile = Join-Path $targetPath ".git"
    if (-not (Test-Path $gitFile)) {
        if (-not $Force) {
            throw "Target path '$targetPath' is not a git worktree. Removal REFUSED to prevent data loss. Use -Force to override."
        }
    }

    # Check 1: Dirty Working Tree Safety Check
    $rawStatus = git -C $targetPath status --porcelain 2>&1
    $statusOutput = @()
    if ($LASTEXITCODE -eq 0 -and $rawStatus.Count -gt 0) {
        $statusOutput = $rawStatus | Where-Object { $_ -notmatch '^\?\?\s+\.cyrene-snapshot\.json$' -and -not [string]::IsNullOrWhiteSpace($_) }
    }

    if ($statusOutput.Count -gt 0) {
        if (-not $Force) {
            Write-Host "`n[DIRTY WORKTREE DETECTED]" -ForegroundColor Red
            $statusOutput | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
            throw "Worktree at '$targetPath' has uncommitted changes or untracked files. Removal REFUSED to prevent data loss. Commit/stash your changes or specify -Force."
        } else {
            Write-Warning "Worktree is dirty, but proceeding because -Force was specified."
        }
    }

    # Check 2: Unpushed Commits Safety Check (for writer branches)
    if ($targetEntry -and $targetEntry.mode -eq "writer") {
        $hasUpstream = (git -C $targetPath rev-parse --abbrev-ref "@{u}" 2>$null)
        $unpushed = $null
        if ($hasUpstream) {
            $unpushed = git -C $targetPath log "@{u}..HEAD" --oneline 2>$null
        } else {
            $baseSha = if ($targetEntry.baseSha) { $targetEntry.baseSha } else { "origin/develop" }
            $unpushed = git -C $targetPath log "$baseSha..HEAD" --oneline 2>$null
        }

        if ($unpushed -and $unpushed.Count -gt 0) {
            if (-not $Force) {
                Write-Host "`n[UNPUSHED COMMITS DETECTED]" -ForegroundColor Red
                $unpushed | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
                throw "Worktree at '$targetPath' contains unpushed commits. Removal REFUSED to prevent data loss. Push your branch or specify -Force."
            } else {
                Write-Warning "Worktree has unpushed commits, but proceeding because -Force was specified."
            }
        }
    }

    # Find the parent repository
    $parentRepo = $null
    if ($targetEntry) {
        try {
            $parentRepo = Resolve-Repository $targetEntry.repo
        } catch {
            if ($targetEntry.repoPath -and (Test-Path $targetEntry.repoPath)) {
                $parentRepo = [PSCustomObject]@{ FullPath = (Resolve-Path $targetEntry.repoPath).Path; Name = $targetEntry.repo }
            }
        }
    }
    if (-not $parentRepo -and (Test-Path $gitFile)) {
        # Read gitdir from .git file
        $gitContent = Get-Content $gitFile -Raw
        if ($gitContent -match 'gitdir:\s*(.+)') {
            $gitDir = $matches[1].Trim()
            $repoDir = Split-Path (Split-Path $gitDir -Parent) -Parent
            $parentRepo = [PSCustomObject]@{ FullPath = $repoDir; Name = (Split-Path $repoDir -Leaf) }
        }
    }

    if ($parentRepo -and (Test-Path $parentRepo.FullPath)) {
        Write-Host "  Removing worktree from Git index..." -ForegroundColor DarkGray
        $rmArgs = @("worktree", "remove", "$targetPath")
        if ($Force) { $rmArgs += "--force" }
        git -C $parentRepo.FullPath @rmArgs 2>$null | Out-Null
    }

    # Clean up directory if still present
    if (Test-Path $targetPath) {
        Remove-Item -Path $targetPath -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Unregister metadata
    Remove-RegistryEntry $worktreeRoot $targetPath $(if ($targetEntry) { $targetEntry.role } else { $Role })

    Write-Host "  [SUCCESS] Worktree at '$targetPath' safely removed." -ForegroundColor Green
}

# --- Main Dispatch ---

switch ($Action) {
    "create"  { Invoke-CreateTaskWorktree }
    "snapshot"{ Invoke-CreateSnapshotWorktree }
    "status"  { Invoke-WorktreeStatus }
    "list"    { Invoke-WorktreeStatus }
    "remove"  { Invoke-RemoveWorktree }
    "cleanup" { Invoke-RemoveWorktree }
    default   { throw "Unknown action '$Action'. Valid actions: create, snapshot, status, remove." }
}
