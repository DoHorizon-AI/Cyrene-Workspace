<#
.SYNOPSIS
    Safe parallel-agent worktree helper for the Cyrene Workspace.

.DESCRIPTION
    PowerShell is the canonical Windows entry point. A writer gets a dedicated
    branch/worktree. A consumer gets a detached exact-SHA integration snapshot.

    The registry is intentionally stored under the runtime worktree root, outside
    this repository. It is local coordination metadata, not topology authority.
    repositories.yaml remains the only committed repository-topology authority.

.EXAMPLE
    .\agent-worktree.ps1 create -Repo Cyrene-Plugins-Official -Branch chore/spring-boot-4-1-1 -Base origin/develop -Role idea-spring

.EXAMPLE
    .\agent-worktree.ps1 snapshot -Repo Cyrene-Platform -Sha 4449fe770ece8bf2ef27271b109dcd8b715f869a -Role rider-media-platform

.EXAMPLE
    .\agent-worktree.ps1 status

.EXAMPLE
    .\agent-worktree.ps1 remove -Role idea-spring

.NOTES
    -Force is an explicit destructive confirmation. It may discard dirty files
    and unpushed commits in the selected worktree. It never resets, force-pushes,
    deletes branches, or removes an unrecognised non-worktree directory.
    Detached snapshots are exact-SHA worktrees, not OS-level read-only filesystems.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("create", "snapshot", "status", "list", "remove", "cleanup", "repair")]
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
$ScriptDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$ManifestPath = Join-Path $ScriptDir "repositories.yaml"
$RegistryFileName = ".registry.json"
$SnapshotMarkerName = ".cyrene-snapshot.json"
$IsWindowsHost = ($env:OS -eq "Windows_NT")

function Format-GitArguments([string[]]$Arguments) {
    return ($Arguments | ForEach-Object { "'$_'" }) -join " "
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$AllowFailure
    )

    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $stdout = @(& git -C $RepositoryPath @Arguments 2> $stderrPath | ForEach-Object { [string]$_ })
        $stderr = @()
        if (Test-Path -LiteralPath $stderrPath) {
            $stderr = @(Get-Content -LiteralPath $stderrPath | ForEach-Object { [string]$_ })
        }
    } finally {
        if (Test-Path -LiteralPath $stderrPath) {
            Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
        }
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $details = (@($stdout) + @($stderr) -join ([Environment]::NewLine)).Trim()
        if ([string]::IsNullOrWhiteSpace($details)) {
            $details = "no diagnostic output"
        }
        throw "git -C '$RepositoryPath' $(Format-GitArguments $Arguments) failed (exit $exitCode): $details"
    }

    return [PSCustomObject]@{
        ExitCode = $exitCode
        Lines = @($stdout)
        Stderr = @($stderr)
    }
}

function Get-GitText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $result = Invoke-Git -RepositoryPath $RepositoryPath -Arguments $Arguments -AllowFailure
    if ($result.ExitCode -ne 0) {
        return $null
    }
    return (($result.Lines -join ([Environment]::NewLine)).Trim())
}

function Normalize-PathValue([string]$Value) {
    $full = [System.IO.Path]::GetFullPath($Value)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([char[]]@("\", "/"))
    }
    return $full
}

function Test-PathEqual([string]$Left, [string]$Right) {
    $comparison = if ($IsWindowsHost) {
        [System.StringComparison]::OrdinalIgnoreCase
    } else {
        [System.StringComparison]::Ordinal
    }
    return [System.String]::Equals(
        (Normalize-PathValue $Left),
        (Normalize-PathValue $Right),
        $comparison
    )
}

function Test-PathUnderRoot([string]$Candidate, [string]$CandidateRoot) {
    $candidateValue = Normalize-PathValue $Candidate
    $rootValue = Normalize-PathValue $CandidateRoot
    if (Test-PathEqual $candidateValue $rootValue) {
        return $true
    }
    $comparison = if ($IsWindowsHost) {
        [System.StringComparison]::OrdinalIgnoreCase
    } else {
        [System.StringComparison]::Ordinal
    }
    return $candidateValue.StartsWith(
        ($rootValue + [System.IO.Path]::DirectorySeparatorChar),
        $comparison
    )
}

function Ensure-DirectoryWritable([string]$DirectoryPath) {
    if (-not (Test-Path -LiteralPath $DirectoryPath)) {
        New-Item -ItemType Directory -Path $DirectoryPath -Force | Out-Null
    }
    if (-not (Get-Item -LiteralPath $DirectoryPath).PSIsContainer) {
        throw "Worktree root '$DirectoryPath' is not a directory."
    }

    $probe = Join-Path $DirectoryPath (".write-test-" + [Guid]::NewGuid().ToString("N"))
    try {
        [System.IO.File]::WriteAllText($probe, "cyrene-worktree-probe")
        return (Normalize-PathValue $DirectoryPath)
    } catch {
        throw "Worktree root '$DirectoryPath' is not writable: $($_.Exception.Message)"
    } finally {
        if (Test-Path -LiteralPath $probe) {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-WorktreeRoot([string]$ExplicitRoot) {
    $requested = $ExplicitRoot
    if ([string]::IsNullOrWhiteSpace($requested)) {
        $requested = $env:CYRENE_WORKTREE_ROOT
    }
    if (-not [string]::IsNullOrWhiteSpace($requested)) {
        return (Ensure-DirectoryWritable (Normalize-PathValue $requested))
    }

    $candidates = @()
    if ($IsWindowsHost) {
        $systemDrive = if ([string]::IsNullOrWhiteSpace($env:SystemDrive)) { "C:" } else { $env:SystemDrive }
        $candidates += ($systemDrive.TrimEnd("\", "/") + "\cwt")
        $candidates += (Join-Path ([System.IO.Path]::GetTempPath()) "cwt")
    } else {
        $homeDir = if (-not [string]::IsNullOrWhiteSpace($env:HOME)) { $env:HOME } else { "" }
        if (-not [string]::IsNullOrWhiteSpace($homeDir)) {
            $candidates += (Join-Path $homeDir ".cyrene/cwt")
        }
        $candidates += "/tmp/cwt"
    }

    foreach ($candidate in $candidates) {
        try {
            return (Ensure-DirectoryWritable (Normalize-PathValue $candidate))
        } catch {
            continue
        }
    }
    throw "No writable short worktree root was found. Set CYRENE_WORKTREE_ROOT to a writable path."
}

function Read-YamlQuotedValue([string]$Line, [string]$Key) {
    $pattern = "^\s+$([Regex]::Escape($Key)):\s*['""]([^'""]+)['""]\s*$"
    if ($Line -match $pattern) {
        return $matches[1]
    }
    return $null
}

function Get-WorkspaceRepositories {
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "repositories.yaml not found at '$ManifestPath'."
    }

    $repositories = @()
    $current = $null
    foreach ($line in (Get-Content -LiteralPath $ManifestPath)) {
        if ($line -match '^\s*-\s*name:\s*["'']([^"'']+)["'']\s*$') {
            if ($null -ne $current) {
                $repositories += [PSCustomObject]$current
            }
            $current = @{
                Name = $matches[1]
                Path = $null
                CanonicalRemote = $null
                IntegrationBranch = "develop"
            }
            continue
        }
        if ($null -eq $current) {
            continue
        }

        $value = Read-YamlQuotedValue $line "path"
        if ($null -ne $value) {
            $current.Path = $value
            continue
        }
        $value = Read-YamlQuotedValue $line "canonical_remote"
        if ($null -ne $value) {
            $current.CanonicalRemote = $value
            continue
        }
        $value = Read-YamlQuotedValue $line "integration_branch"
        if ($null -ne $value) {
            $current.IntegrationBranch = $value
        }
    }
    if ($null -ne $current) {
        $repositories += [PSCustomObject]$current
    }

    foreach ($repository in $repositories) {
        if ([string]::IsNullOrWhiteSpace($repository.Path)) {
            throw "Repository '$($repository.Name)' has no path in repositories.yaml."
        }
        $repository | Add-Member -NotePropertyName FullPath -NotePropertyValue (
            Normalize-PathValue (Join-Path $ScriptDir $repository.Path)
        ) -Force
    }
    return @($repositories)
}

function Get-GitTopLevel([string]$CandidatePath) {
    $fullCandidate = Normalize-PathValue $CandidatePath
    if (-not (Test-Path -LiteralPath $fullCandidate)) {
        return $null
    }
    $topLevel = Get-GitText $fullCandidate @("rev-parse", "--show-toplevel")
    if ([string]::IsNullOrWhiteSpace($topLevel)) {
        return $null
    }
    return (Normalize-PathValue $topLevel)
}

function Resolve-Repository([string]$Query) {
    if ([string]::IsNullOrWhiteSpace($Query)) {
        throw "Repository (-Repo) must be specified."
    }

    $repositories = @(Get-WorkspaceRepositories)
    $match = @($repositories | Where-Object { $_.Name -ceq $Query })
    if ($match.Count -eq 1) {
        return $match[0]
    }

    $match = @($repositories | Where-Object { $_.Name -ieq $Query })
    if ($match.Count -eq 1) {
        return $match[0]
    }

    $match = @($repositories | Where-Object {
        (Split-Path $_.Path -Leaf) -ieq $Query
    })
    if ($match.Count -eq 1) {
        return $match[0]
    }

    $aliases = @{
        "platform"  = "Cyrene-Platform"
        "plugins"   = "Cyrene-Plugins-Official"
        "reactor"   = "Cyrene-Reactor"
        "yield"     = "Cyrene-Yield"
        "exchange"  = "Cyrene-Exchange"
        "catalyst"  = "Cyrene-Catalyst"
        "echo"      = "Cyrene-Echo"
        "navigator" = "Cyrene-Navigator"
    }
    $aliasKey = $Query.Trim().ToLowerInvariant()
    if ($aliases.ContainsKey($aliasKey)) {
        $match = @($repositories | Where-Object { $_.Name -ceq $aliases[$aliasKey] })
        if ($match.Count -eq 1) {
            return $match[0]
        }
    }

    $directPath = $null
    if (Test-Path -LiteralPath $Query) {
        $directPath = Normalize-PathValue $Query
    }
    if ($null -ne $directPath) {
        $topLevel = Get-GitTopLevel $directPath
        if ($null -ne $topLevel) {
            return [PSCustomObject]@{
                Name = Split-Path $topLevel -Leaf
                Path = $Query
                FullPath = $topLevel
                CanonicalRemote = $null
                IntegrationBranch = "develop"
            }
        }
    }

    $available = ($repositories | ForEach-Object { $_.Name }) -join ", "
    throw "Repository '$Query' not found in repositories.yaml. Available: $available"
}

function Get-RegistryPath([string]$WorktreeRoot) {
    return (Join-Path $WorktreeRoot $RegistryFileName)
}

function Repair-RegistryEntries([string]$WorktreeRoot) {
    $registryPath = Get-RegistryPath $WorktreeRoot
    Write-Warning "Registry at '$registryPath' is invalid or missing; repairing from active Git worktree inventory..."
    
    $recoveredEntries = @()
    $inventory = @(Get-AllWorktreeInventory)
    foreach ($wt in $inventory) {
        if ($wt.IsPrimary) { continue }
        if (-not (Test-PathUnderRoot $wt.Path $WorktreeRoot)) { continue }
        
        $markerPath = Join-Path $wt.Path $SnapshotMarkerName
        $marker = $null
        if (Test-Path -LiteralPath $markerPath) {
            try { $marker = ConvertFrom-Json (Get-Content -LiteralPath $markerPath -Raw) } catch {}
        }
        
        $mode = if ($wt.Detached -or ($null -ne $marker -and $marker.type -eq "CYRENE_INTEGRATION_SNAPSHOT")) { "snapshot" } else { "writer" }
        $role = if ($null -ne $marker -and -not [string]::IsNullOrWhiteSpace($marker.role)) {
            $marker.role
        } else {
            Split-Path $wt.Path -Leaf
        }
        
        $recoveredEntries += [ordered]@{
            role = $role
            repository = $wt.Repository.Name
            repositoryPath = $wt.Repository.Path
            path = $wt.Path
            mode = $mode
            branch = if ($mode -eq "writer") { $wt.Branch } else { $null }
            baseRef = $null
            baseSha = $null
            integrationSha = $wt.Head
            createdAt = [DateTime]::UtcNow.ToString("o")
            repaired = $true
        }
    }
    
    Save-RegistryEntries $WorktreeRoot $recoveredEntries
    return @($recoveredEntries)
}

function Get-RegistryEntries([string]$WorktreeRoot) {
    $registryPath = Get-RegistryPath $WorktreeRoot
    if (-not (Test-Path -LiteralPath $registryPath)) {
        return @()
    }

    try {
        $raw = Get-Content -LiteralPath $registryPath -Raw
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return (Repair-RegistryEntries $WorktreeRoot)
        }
        $payload = ConvertFrom-Json $raw
    } catch {
        return (Repair-RegistryEntries $WorktreeRoot)
    }
    if ($null -eq $payload) {
        return (Repair-RegistryEntries $WorktreeRoot)
    }
    if ($payload -is [System.Array]) {
        return @($payload)
    }
    if ($payload.PSObject.Properties.Name -contains "entries") {
        return @($payload.entries)
    }
    return (Repair-RegistryEntries $WorktreeRoot)
}

function Save-RegistryEntries([string]$WorktreeRoot, [object[]]$Entries) {
    $payload = [ordered]@{
        schemaVersion = 1
        updatedAt = [DateTime]::UtcNow.ToString("o")
        entries = @($Entries)
    }
    $json = ConvertTo-Json -InputObject $payload -Depth 10
    [System.IO.File]::WriteAllText(
        (Get-RegistryPath $WorktreeRoot),
        $json,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Get-EntryRepositoryName($Entry) {
    if ($null -ne $Entry.repository) {
        return [string]$Entry.repository
    }
    if ($null -ne $Entry.repo) {
        return [string]$Entry.repo
    }
    return $null
}

function Get-EntryIntegrationSha($Entry) {
    if ($null -ne $Entry.integrationSha) {
        return [string]$Entry.integrationSha
    }
    if ($null -ne $Entry.sha) {
        return [string]$Entry.sha
    }
    return $null
}

function Find-RegistryEntryByRole([object[]]$Entries, [string]$AgentRole) {
    return @($Entries | Where-Object { [string]$_.role -eq $AgentRole })
}

function Find-RegistryEntryByPath([object[]]$Entries, [string]$TargetPath) {
    return @($Entries | Where-Object {
        $null -ne $_.path -and (Test-PathEqual ([string]$_.path) $TargetPath)
    })
}

function Add-RegistryEntry([string]$WorktreeRoot, $Entry) {
    $entries = @(Get-RegistryEntries $WorktreeRoot)
    $sameRole = @(Find-RegistryEntryByRole $entries ([string]$Entry.role))
    foreach ($existing in $sameRole) {
        if (
            (Test-PathEqual ([string]$existing.path) ([string]$Entry.path)) -and
            ((Get-EntryRepositoryName $existing) -eq (Get-EntryRepositoryName $Entry)) -and
            ([string]$existing.mode -eq [string]$Entry.mode)
        ) {
            return
        }
        throw "Role '$($Entry.role)' is already registered for another worktree; refusing to overwrite ownership metadata."
    }

    $samePath = @(Find-RegistryEntryByPath $entries ([string]$Entry.path))
    foreach ($existing in $samePath) {
        if ([string]$existing.role -ne [string]$Entry.role) {
            if ([string]$existing.mode -eq "snapshot" -and [string]$Entry.mode -eq "snapshot") {
                continue
            }
            throw "Path '$($Entry.path)' is already registered to role '$($existing.role)'."
        }
    }

    Save-RegistryEntries $WorktreeRoot (@($entries) + @($Entry))
}

function Remove-RegistryEntry([string]$WorktreeRoot, [string]$TargetPath, [string]$Role = $null) {
    $entries = @(Get-RegistryEntries $WorktreeRoot)
    $remaining = @($entries | Where-Object {
        if (-not [string]::IsNullOrWhiteSpace($Role) -and [string]$_.role -eq $Role) {
            return $false
        }
        if ([string]::IsNullOrWhiteSpace($Role) -and $null -ne $_.path -and (Test-PathEqual ([string]$_.path) $TargetPath)) {
            return $false
        }
        return $true
    })
    Save-RegistryEntries $WorktreeRoot $remaining
}

function Get-WorktreeInventory($Repository) {
    if ($null -eq $Repository -or [string]::IsNullOrWhiteSpace($Repository.FullPath) -or -not (Test-Path -LiteralPath $Repository.FullPath)) {
        return @()
    }
    $result = Invoke-Git $Repository.FullPath @("worktree", "list", "--porcelain") -AllowFailure
    if ($result.ExitCode -ne 0) {
        return @()
    }
    $records = @()
    $current = $null

    foreach ($line in $result.Lines) {
        $text = [string]$line
        if ([string]::IsNullOrWhiteSpace($text)) {
            if ($null -ne $current) {
                $records += [PSCustomObject]$current
                $current = $null
            }
            continue
        }
        if ($text -match '^worktree\s+(.+)$') {
            if ($null -ne $current) {
                $records += [PSCustomObject]$current
            }
            $current = @{
                Repository = $Repository
                Path = $matches[1].Trim()
                Head = $null
                Branch = $null
                Detached = $false
            }
            continue
        }
        if ($null -eq $current) {
            continue
        }
        if ($text -match '^HEAD\s+([0-9a-fA-F]{40})$') {
            $current.Head = $matches[1].ToLowerInvariant()
        } elseif ($text -match '^branch\s+refs/heads/(.+)$') {
            $current.Branch = $matches[1].Trim()
        } elseif ($text -eq "detached") {
            $current.Detached = $true
        }
    }
    if ($null -ne $current) {
        $records += [PSCustomObject]$current
    }

    foreach ($record in $records) {
        $record.Path = Normalize-PathValue $record.Path
        if ([string]::IsNullOrWhiteSpace($record.Branch)) {
            $record.Detached = $true
        }
        $record | Add-Member -NotePropertyName IsPrimary -NotePropertyValue (
            Test-PathEqual $record.Path $Repository.FullPath
        ) -Force
    }
    return @($records)
}

function Get-AllWorktreeInventory {
    $all = @()
    foreach ($repository in (Get-WorkspaceRepositories)) {
        if ($null -ne (Get-GitTopLevel $repository.FullPath)) {
            $all += @(Get-WorktreeInventory $repository)
        }
    }
    return @($all)
}

function Get-TargetPath([string]$WorktreeRoot, [string]$RequestedPath, [string]$AgentRole) {
    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        if ([System.IO.Path]::IsPathRooted($RequestedPath)) {
            return (Normalize-PathValue $RequestedPath)
        }
        return (Normalize-PathValue (Join-Path $WorktreeRoot $RequestedPath))
    }
    return (Normalize-PathValue (Join-Path $WorktreeRoot $AgentRole))
}

function Assert-ValidRole([string]$AgentRole) {
    if ([string]::IsNullOrWhiteSpace($AgentRole)) {
        throw "Parameter -Role is required."
    }
    if ($AgentRole -match '[\\/]') {
        throw "Role '$AgentRole' must be a single directory name, not a path."
    }
    if ($AgentRole -eq "." -or $AgentRole -eq "..") {
        throw "Role '$AgentRole' is not a valid worktree name."
    }
    if ($AgentRole.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "Role '$AgentRole' contains invalid path characters."
    }
}

function Assert-ValidBranch([string]$RepositoryPath, [string]$BranchName) {
    if ([string]::IsNullOrWhiteSpace($BranchName)) {
        throw "Parameter -Branch is required for create."
    }
    $result = Invoke-Git $RepositoryPath @("check-ref-format", "--branch", $BranchName) -AllowFailure
    if ($result.ExitCode -ne 0) {
        throw "Branch '$BranchName' is not a valid local branch name."
    }
}

function Invoke-SafeFetch($Repository) {
    $remoteNames = @(Invoke-Git $Repository.FullPath @("remote")).Lines
    if (-not (@($remoteNames | Where-Object { $_ -eq "origin" }).Count -gt 0)) {
        return
    }
    $result = Invoke-Git $Repository.FullPath @("fetch", "--no-tags", "origin", "--prune") -AllowFailure
    if ($result.ExitCode -ne 0) {
        $details = ($result.Stderr -join ([Environment]::NewLine)).Trim()
        throw "Safe fetch from origin failed for '$($Repository.Name)'; refusing to use a possibly stale integration baseline. $details"
    }
}

function Resolve-Commit([string]$RepositoryPath, [string]$Reference) {
    if ([string]::IsNullOrWhiteSpace($Reference)) {
        return $null
    }
    $result = Invoke-Git $RepositoryPath @(
        "rev-parse", "--verify", "--end-of-options", "$Reference^{commit}"
    ) -AllowFailure
    if ($result.ExitCode -ne 0 -or $result.Lines.Count -eq 0) {
        return $null
    }
    $resolved = ([string]$result.Lines[0]).Trim().ToLowerInvariant()
    if ($resolved -notmatch '^[0-9a-f]{40}$') {
        return $null
    }
    return $resolved
}

function Resolve-Base($Repository) {
    Invoke-SafeFetch $Repository
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Base)) {
        $candidates = @($Base)
    } else {
        $candidates = @()
        $remoteNames = @(Invoke-Git $Repository.FullPath @("remote")).Lines
        if (@($remoteNames | Where-Object { $_ -eq "origin" }).Count -gt 0) {
            $candidates += "origin/$($Repository.IntegrationBranch)"
        }
        $candidates += $Repository.IntegrationBranch
    }

    foreach ($candidate in $candidates) {
        $resolved = Resolve-Commit $Repository.FullPath $candidate
        if ($null -ne $resolved) {
            return [PSCustomObject]@{
                Reference = $candidate
                Sha = $resolved
            }
        }
    }
    throw "Could not resolve the requested base '$($candidates -join ", ")' to a commit in '$($Repository.Name)'."
}

function Assert-ExactSha([string]$ShaValue) {
    if ($ShaValue -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Snapshot -Sha must be a full 40-character commit SHA; refs and abbreviated SHAs are refused."
    }
}

function Test-SnapshotMarker([string]$WorktreePath, $Entry) {
    $markerPath = Join-Path $WorktreePath $SnapshotMarkerName
    if (-not (Test-Path -LiteralPath $markerPath)) {
        return $false
    }
    try {
        $marker = ConvertFrom-Json (Get-Content -LiteralPath $markerPath -Raw)
    } catch {
        return $false
    }
    $expectedSha = if ($null -ne $Entry) { Get-EntryIntegrationSha $Entry } else { $null }
    if ([string]$marker.type -ne "CYRENE_INTEGRATION_SNAPSHOT") {
        return $false
    }
    if ([string]$marker.mutability -ne "detached-exact-sha") {
        return $false
    }
    if ($marker.filesystemReadOnly -ne $false) {
        return $false
    }
    if ($null -ne $expectedSha -and [string]$marker.sha -ieq $expectedSha) {
        return $true
    }
    return ($null -eq $expectedSha -and [string]$marker.sha -match '^[0-9a-fA-F]{40}$')
}

function Write-SnapshotMarker([string]$WorktreePath, [string]$AgentRole, [string]$RepositoryName, [string]$CommitSha) {
    $payload = [ordered]@{
        type = "CYRENE_INTEGRATION_SNAPSHOT"
        role = $AgentRole
        repository = $RepositoryName
        sha = $CommitSha
        mutability = "detached-exact-sha"
        filesystemReadOnly = $false
        warning = "Cross-Agent integration snapshot. Detached at this exact SHA; filesystem read-only enforcement is not enabled."
        createdAt = [DateTime]::UtcNow.ToString("o")
    }
    $json = ConvertTo-Json -InputObject $payload -Depth 5
    [System.IO.File]::WriteAllText(
        (Join-Path $WorktreePath $SnapshotMarkerName),
        $json,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Get-DirtyLines([string]$WorktreePath, [string]$Mode, $Entry) {
    if (-not (Test-Path -LiteralPath $WorktreePath)) {
        return @()
    }
    $result = Invoke-Git $WorktreePath @("status", "--porcelain=v1", "--untracked-files=all") -AllowFailure
    if ($result.ExitCode -ne 0) {
        return @()
    }
    $lines = @($result.Lines | Where-Object {
        $_ -match '^[ MADRCU?]{2}\s' -and $_ -notmatch '^!!'
    })
    if (
        $Mode -eq "snapshot" -or $Mode -eq "MANAGED_SNAPSHOT" -and
        (Test-SnapshotMarker $WorktreePath $Entry)
    ) {
        $lines = @($lines | Where-Object {
            $_ -notmatch '^\?\?\s+\.cyrene-snapshot\.json$'
        })
    }
    return @($lines)
}

function Get-UnpushedLines([string]$WorktreePath, $Entry) {
    if (-not (Test-Path -LiteralPath $WorktreePath)) {
        return @()
    }
    $upstreamResult = Invoke-Git $WorktreePath @(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    ) -AllowFailure
    if ($upstreamResult.ExitCode -eq 0 -and $upstreamResult.Lines.Count -gt 0) {
        $upstream = ([string]$upstreamResult.Lines[0]).Trim()
        $logResult = Invoke-Git $WorktreePath @(
            "log", "--oneline", "$upstream..HEAD"
        ) -AllowFailure
        if ($logResult.ExitCode -eq 0) {
            return @($logResult.Lines)
        }
    }

    $baseSha = if ($null -ne $Entry) { [string]$Entry.baseSha } else { $null }
    if ([string]::IsNullOrWhiteSpace($baseSha)) {
        return @("No upstream or recorded base SHA is available.")
    }

    $ancestor = Invoke-Git $WorktreePath @(
        "merge-base", "--is-ancestor", $baseSha, "HEAD"
    ) -AllowFailure
    if ($ancestor.ExitCode -ne 0) {
        return @("Recorded base SHA $baseSha is not an ancestor of HEAD.")
    }
    $logResult = Invoke-Git $WorktreePath @(
        "log", "--oneline", "$baseSha..HEAD"
    ) -AllowFailure
    if ($logResult.ExitCode -eq 0) {
        return @($logResult.Lines)
    }
    return @()
}

function Get-EntryState($Worktree, [string]$Mode, $Entry) {
    if ($null -eq $Worktree -or -not (Test-Path -LiteralPath $Worktree.Path)) {
        return "missing"
    }
    $states = @()
    if ($Mode -eq "snapshot" -or $Mode -eq "MANAGED_SNAPSHOT") {
        $expectedSha = Get-EntryIntegrationSha $Entry
        if (
            -not $Worktree.Detached -or
            [string]::IsNullOrWhiteSpace($expectedSha) -or
            $Worktree.Head -ine $expectedSha -or
            -not (Test-SnapshotMarker $Worktree.Path $Entry)
        ) {
            $states += "invalid"
        }
    }
    $dirty = @(Get-DirtyLines $Worktree.Path $Mode $Entry)
    if ($dirty.Count -gt 0) {
        $states += "dirty"
    }
    if ($Mode -eq "writer" -or $Mode -eq "MANAGED_WRITER_WORKTREE") {
        $unpushed = @(Get-UnpushedLines $Worktree.Path $Entry)
        if ($unpushed.Count -gt 0) {
            $states += "unpushed"
        }
    }
    if ($states.Count -eq 0) {
        return "clean"
    }
    return ($states -join "+")
}

function Get-ShortSha([string]$CommitSha) {
    if ([string]::IsNullOrWhiteSpace($CommitSha)) {
        return "?"
    }
    return $CommitSha.Substring(0, [Math]::Min(7, $CommitSha.Length))
}

function Get-RepositoryForEntry($Entry) {
    $repositoryName = Get-EntryRepositoryName $Entry
    if (-not [string]::IsNullOrWhiteSpace($repositoryName)) {
        try {
            return (Resolve-Repository $repositoryName)
        } catch {
        }
    }
    if ($null -ne $Entry.repositoryPath -and (Test-Path -LiteralPath ([string]$Entry.repositoryPath))) {
        try {
            return (Resolve-Repository ([string]$Entry.repositoryPath))
        } catch {
        }
    }
    return $null
}

function Get-RelevantWorktreeInventory([object[]]$Entries, $ExplicitRepository = $null) {
    $inventory = @(Get-AllWorktreeInventory)
    $repositoryPaths = @($inventory | ForEach-Object {
        Normalize-PathValue $_.Repository.FullPath
    })
    if ($null -ne $ExplicitRepository) {
        $repPath = Normalize-PathValue $ExplicitRepository.FullPath
        $known = @($repositoryPaths | Where-Object { Test-PathEqual $_ $repPath })
        if ($known.Count -eq 0) {
            $inventory += @(Get-WorktreeInventory $ExplicitRepository)
            $repositoryPaths += $repPath
        }
    }
    foreach ($entry in $Entries) {
        $repository = Get-RepositoryForEntry $entry
        if ($null -eq $repository) {
            continue
        }
        $repositoryPath = Normalize-PathValue $repository.FullPath
        $known = @($repositoryPaths | Where-Object { Test-PathEqual $_ $repositoryPath })
        if ($known.Count -eq 0) {
            $inventory += @(Get-WorktreeInventory $repository)
            $repositoryPaths += $repositoryPath
        }
    }
    return @($inventory)
}

function Write-Status {
    $worktreeRoot = Get-WorktreeRoot $Root
    $entries = @(Get-RegistryEntries $worktreeRoot)
    $inventory = @(Get-RelevantWorktreeInventory $entries)
    $rows = @()
    $processedPaths = @()

    foreach ($entry in ($entries | Sort-Object role)) {
        $entryPath = [string]$entry.path
        $processedPaths += (Normalize-PathValue $entryPath)
        $actual = @($inventory | Where-Object { Test-PathEqual $_.Path $entryPath } | Select-Object -First 1)
        $actualWorktree = if ($actual.Count -gt 0) { $actual[0] } else { $null }
        $mode = [string]$entry.mode
        $displayMode = if ($mode -eq "snapshot") { "MANAGED_SNAPSHOT" } else { "MANAGED_WRITER_WORKTREE" }
        $ref = if ($mode -eq "snapshot") {
            Get-ShortSha (Get-EntryIntegrationSha $entry)
        } else {
            [string]$entry.branch
        }
        $state = Get-EntryState $actualWorktree $mode $entry
        $rows += [PSCustomObject]@{
            Role = [string]$entry.role
            Repository = (Get-EntryRepositoryName $entry)
            Mode = $displayMode
            RefSha = $ref
            State = $state
            Path = $entryPath
        }
    }

    # Discover unregistered git worktrees
    foreach ($worktree in $inventory) {
        if ($worktree.IsPrimary) {
            continue
        }
        $normPath = Normalize-PathValue $worktree.Path
        if (-not (Test-PathUnderRoot $normPath $worktreeRoot)) {
            continue
        }
        if ($processedPaths -contains $normPath) {
            continue
        }
        $processedPaths += $normPath

        $marker = $null
        $markerPath = Join-Path $worktree.Path $SnapshotMarkerName
        if (Test-Path -LiteralPath $markerPath) {
            try {
                $marker = ConvertFrom-Json (Get-Content -LiteralPath $markerPath -Raw)
            } catch {
            }
        }
        $discoveredMode = "UNREGISTERED_WORKTREE"
        $discoveredRole = if ($null -ne $marker -and -not [string]::IsNullOrWhiteSpace($marker.role)) {
            "$($marker.role) (unregistered)"
        } else {
            "(unregistered)"
        }
        $rows += [PSCustomObject]@{
            Role = $discoveredRole
            Repository = $worktree.Repository.Name
            Mode = $discoveredMode
            RefSha = if ($worktree.Detached) { Get-ShortSha $worktree.Head } else { if ([string]::IsNullOrWhiteSpace($worktree.Branch)) { "(detached)" } else { $worktree.Branch } }
            State = "unmanaged / needs_review"
            Path = $worktree.Path
        }
    }

    # Discover foreign or unmanaged directories under $worktreeRoot
    if (Test-Path -LiteralPath $worktreeRoot) {
        $childDirs = Get-ChildItem -LiteralPath $worktreeRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                $name = $_.Name
                $name -notin @(".snapshots", ".state", ".gemini", ".agents", "skills") -and -not $name.StartsWith(".")
            }
        foreach ($dir in $childDirs) {
            $normDirPath = Normalize-PathValue $dir.FullName
            if ($processedPaths -contains $normDirPath) {
                continue
            }
            $rows += [PSCustomObject]@{
                Role = "(unmanaged)"
                Repository = "(foreign/unknown)"
                Mode = "FOREIGN_OR_UNMANAGED_DIRECTORY"
                RefSha = "-"
                State = "unmanaged / needs_review"
                Path = $normDirPath
            }
        }
    }

    Write-Output "Resolved worktree root: $worktreeRoot"
    Write-Output ""
    Write-Output "CYRENE PARALLEL AGENT WORKTREE STATUS"
    Write-Output ("{0,-32} {1,-24} {2,-32} {3,-18} {4,-24} {5}" -f "ROLE", "REPO", "MODE", "REF/SHA", "STATE", "PATH")
    Write-Output ("{0,-32} {1,-24} {2,-32} {3,-18} {4,-24} {5}" -f ("-" * 32), ("-" * 24), ("-" * 32), ("-" * 18), ("-" * 24), ("-" * 20))
    foreach ($row in $rows) {
        Write-Output ("{0,-32} {1,-24} {2,-32} {3,-18} {4,-24} {5}" -f
            $row.Role, $row.Repository, $row.Mode, $row.RefSha, $row.State, $row.Path)
    }
}

function Invoke-CreateTaskWorktree {
    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Parameter -Repo is required for create."
    }
    Assert-ValidRole $Role
    $repository = Resolve-Repository $Repo
    Assert-ValidBranch $repository.FullPath $Branch
    $worktreeRoot = Get-WorktreeRoot $Root
    $targetPath = Get-TargetPath $worktreeRoot $Path $Role
    if (-not (Test-PathUnderRoot $targetPath $worktreeRoot)) {
        throw "Target path '$targetPath' must be under the resolved worktree root '$worktreeRoot'."
    }

    $entries = @(Get-RegistryEntries $worktreeRoot)
    $roleEntries = @(Find-RegistryEntryByRole $entries $Role)
    $inventory = @(Get-RelevantWorktreeInventory $entries)
    $targetWorktree = @($inventory | Where-Object { Test-PathEqual $_.Path $targetPath } | Select-Object -First 1)
    $targetActual = if ($targetWorktree.Count -gt 0) { $targetWorktree[0] } else { $null }

    if ($roleEntries.Count -gt 0) {
        $registered = $roleEntries[0]
        if (
            $roleEntries.Count -eq 1 -and
            (Test-PathEqual ([string]$registered.path) $targetPath) -and
            ((Get-EntryRepositoryName $registered) -eq $repository.Name) -and
            ([string]$registered.mode -eq "writer") -and
            $null -ne $targetActual -and
            -not $targetActual.Detached -and
            ([string]$targetActual.Branch -eq $Branch)
        ) {
            Write-Output "Resolved worktree root: $worktreeRoot"
            Write-Output "IDEMPOTENT: role '$Role' already owns writer worktree '$targetPath' on branch '$Branch'."
            return
        }
        throw "Role '$Role' is already registered; refusing to attach or overwrite another agent worktree."
    }

    if ($null -ne $targetActual) {
        throw "Target path '$targetPath' is already a Git worktree; refusing to overwrite it."
    }
    if (Test-Path -LiteralPath $targetPath) {
        throw "Target path '$targetPath' already exists and is not an empty, known worktree destination."
    }

    $sameBranch = @($inventory | Where-Object {
        (Test-PathEqual $_.Repository.FullPath $repository.FullPath) -and
        -not $_.IsPrimary -and
        [string]$_.Branch -eq $Branch
    })
    if ($sameBranch.Count -gt 0) {
        throw "Branch '$Branch' is already checked out in another worktree; refusing to reuse it."
    }
    $branchExists = Invoke-Git $repository.FullPath @(
        "show-ref", "--verify", "--quiet", "refs/heads/$Branch"
    ) -AllowFailure
    if ($branchExists.ExitCode -eq 0) {
        throw "Local branch '$Branch' already exists; refusing to attach or reset it. Choose a new task branch."
    }

    $base = Resolve-Base $repository
    Write-Output "Resolved worktree root: $worktreeRoot"
    Write-Output "Resolved base: $($base.Reference) -> $($base.Sha)"
    Invoke-Git $repository.FullPath @(
        "worktree", "add", "-b", $Branch, $targetPath, $base.Sha
    ) | Out-Null

    $created = @((Get-WorktreeInventory $repository) | Where-Object { Test-PathEqual $_.Path $targetPath })
    if ($created.Count -ne 1 -or $created[0].Detached -or $created[0].Branch -ne $Branch -or $created[0].Head -ine $base.Sha) {
        throw "Created worktree failed postcondition checks; no metadata was recorded."
    }
    $entry = [ordered]@{
        role = $Role
        repository = $repository.Name
        repositoryPath = $repository.Path
        path = $targetPath
        mode = "writer"
        branch = $Branch
        baseRef = $base.Reference
        baseSha = $base.Sha
        integrationSha = $base.Sha
        createdAt = [DateTime]::UtcNow.ToString("o")
    }
    Add-RegistryEntry $worktreeRoot $entry
    Write-Output "Created writer worktree: $targetPath"
    Write-Output "Branch: $Branch"
}

function Invoke-CreateSnapshotWorktree {
    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Parameter -Repo is required for snapshot."
    }
    Assert-ValidRole $Role
    Assert-ExactSha $Sha
    $repository = Resolve-Repository $Repo
    $worktreeRoot = Get-WorktreeRoot $Root
    $targetPath = Get-TargetPath $worktreeRoot $Path $Role
    if (-not (Test-PathUnderRoot $targetPath $worktreeRoot)) {
        throw "Target path '$targetPath' must be under the resolved worktree root '$worktreeRoot'."
    }

    Invoke-SafeFetch $repository
    $resolvedSha = Resolve-Commit $repository.FullPath $Sha
    if ([string]::IsNullOrWhiteSpace($resolvedSha)) {
        throw "Exact commit SHA '$Sha' could not be resolved after safe fetch; refusing to create a floating snapshot."
    }

    $entries = @(Get-RegistryEntries $worktreeRoot)
    $roleEntries = @(Find-RegistryEntryByRole $entries $Role)
    $inventory = @(Get-RelevantWorktreeInventory $entries $repository)
    $targetWorktree = @($inventory | Where-Object { Test-PathEqual $_.Path $targetPath } | Select-Object -First 1)
    $targetActual = if ($targetWorktree.Count -gt 0) { $targetWorktree[0] } else { $null }

    # 1. Exact-SHA Snapshot Cache Check (C:\cwt\.snapshots\<repo>\<sha>)
    $snapshotsDir = Join-Path $worktreeRoot ".snapshots"
    $repoSnapshotDir = Join-Path $snapshotsDir $repository.Name
    $cachedSnapshotPath = Normalize-PathValue (Join-Path $repoSnapshotDir $resolvedSha)
    
    # Check if a verified snapshot already exists in the cache or at targetPath
    $matchingExisting = @($inventory | Where-Object {
        $_.Detached -and
        $_.Head -ieq $resolvedSha -and
        (Test-PathEqual $_.Repository.FullPath $repository.FullPath) -and
        (Test-SnapshotMarker $_.Path $null) -and
        ((Get-DirtyLines $_.Path "snapshot" $null).Count -eq 0)
    })

    if ($matchingExisting.Count -gt 0) {
        $existingSnapshot = $matchingExisting[0]
        # Check if role is already registered
        if ($roleEntries.Count -gt 0) {
            $registered = $roleEntries[0]
            if (
                (Test-PathEqual ([string]$registered.path) $existingSnapshot.Path) -and
                ((Get-EntryRepositoryName $registered) -eq $repository.Name) -and
                ([string]$registered.mode -eq "snapshot")
            ) {
                Write-Output "Resolved worktree root: $worktreeRoot"
                Write-Output "IDEMPOTENT: role '$Role' already owns exact snapshot $resolvedSha at '$($existingSnapshot.Path)'."
                return
            }
        }

        # Role can safely bind to existing verified snapshot
        if ($roleEntries.Count -eq 0) {
            $entry = [ordered]@{
                role = $Role
                repository = $repository.Name
                repositoryPath = $repository.FullPath
                path = $existingSnapshot.Path
                mode = "snapshot"
                branch = $null
                baseRef = $null
                baseSha = $null
                integrationSha = $resolvedSha
                createdAt = [DateTime]::UtcNow.ToString("o")
            }
            Add-RegistryEntry $worktreeRoot $entry
            Write-Output "Resolved worktree root: $worktreeRoot"
            Write-Output "REUSED_SNAPSHOT: Attached role '$Role' to verified snapshot $resolvedSha at '$($existingSnapshot.Path)'."
            return
        }
    }

    if ($roleEntries.Count -gt 0) {
        $registered = $roleEntries[0]
        if (
            $roleEntries.Count -eq 1 -and
            (Test-PathEqual ([string]$registered.path) $targetPath) -and
            ((Get-EntryRepositoryName $registered) -eq $repository.Name) -and
            ([string]$registered.mode -eq "snapshot") -and
            $null -ne $targetActual -and
            $targetActual.Detached -and
            $targetActual.Head -ieq $resolvedSha -and
            (Test-SnapshotMarker $targetPath $registered)
        ) {
            Write-Output "Resolved worktree root: $worktreeRoot"
            Write-Output "IDEMPOTENT: role '$Role' already owns exact snapshot $resolvedSha."
            return
        }
        throw "Role '$Role' is already registered; refusing to replace another agent snapshot."
    }

    if ($null -ne $targetActual) {
        throw "Target path '$targetPath' is already a Git worktree; refusing to overwrite it."
    }
    if (Test-Path -LiteralPath $targetPath) {
        throw "Target path '$targetPath' already exists and is not an empty, known worktree destination."
    }

    Write-Output "Resolved worktree root: $worktreeRoot"
    Write-Output "Resolved exact SHA: $resolvedSha"
    Invoke-Git $repository.FullPath @(
        "worktree", "add", "--detach", $targetPath, $resolvedSha
    ) | Out-Null
    Write-SnapshotMarker $targetPath $Role $repository.Name $resolvedSha

    $created = @((Get-WorktreeInventory $repository) | Where-Object { Test-PathEqual $_.Path $targetPath })
    $symbolicRef = Get-GitText $targetPath @("symbolic-ref", "--quiet", "--short", "HEAD")
    if (
        $created.Count -ne 1 -or
        -not $created[0].Detached -or
        $created[0].Head -ine $resolvedSha -or
        -not [string]::IsNullOrWhiteSpace($symbolicRef)
    ) {
        throw "Snapshot failed detached exact-SHA postcondition checks; no metadata was recorded."
    }
    $entry = [ordered]@{
        role = $Role
        repository = $repository.Name
        repositoryPath = $repository.FullPath
        path = $targetPath
        mode = "snapshot"
        branch = $null
        baseRef = $null
        baseSha = $null
        integrationSha = $resolvedSha
        createdAt = [DateTime]::UtcNow.ToString("o")
    }
    Add-RegistryEntry $worktreeRoot $entry
    Write-Output "Created detached exact-SHA integration snapshot: $targetPath"
    Write-Output "Filesystem read-only enforcement: false"
}

function Invoke-RemoveWorktree {
    if ([string]::IsNullOrWhiteSpace($Role) -and [string]::IsNullOrWhiteSpace($Path)) {
        throw "Either -Role or -Path must be specified for remove/cleanup."
    }

    $worktreeRoot = Get-WorktreeRoot $Root
    $entries = @(Get-RegistryEntries $worktreeRoot)
    $roleMatches = @()
    if (-not [string]::IsNullOrWhiteSpace($Role)) {
        $roleMatches = @(Find-RegistryEntryByRole $entries $Role)
        if ($roleMatches.Count -gt 1) {
            throw "Role '$Role' has duplicate registry entries; refusing cleanup."
        }
    }
    $requestedTarget = Get-TargetPath $worktreeRoot $Path $Role
    $registered = if ($roleMatches.Count -eq 1) {
        $roleMatches[0]
    } else {
        $pathMatches = @(Find-RegistryEntryByPath $entries $requestedTarget)
        if ($pathMatches.Count -gt 1) {
            throw "Path '$requestedTarget' has duplicate registry entries; refusing cleanup."
        }
        if ($pathMatches.Count -eq 1) { $pathMatches[0] } else { $null }
    }
    if ($null -ne $registered) {
        $registeredTarget = Normalize-PathValue ([string]$registered.path)
        if (-not [string]::IsNullOrWhiteSpace($Path) -and -not (Test-PathEqual $requestedTarget $registeredTarget)) {
            throw "Role/path selection does not match the registered worktree; refusing cleanup."
        }
        $requestedTarget = $registeredTarget
    }

    if (-not (Test-PathUnderRoot $requestedTarget $worktreeRoot)) {
        throw "Cleanup target '$requestedTarget' is outside the resolved worktree root."
    }

    # Case 11: Missing worktree path on disk with stale registry entry
    if (-not (Test-Path -LiteralPath $requestedTarget)) {
        if ($null -ne $registered) {
            Remove-RegistryEntry $worktreeRoot $requestedTarget ([string]$registered.role)
            Write-Output "Removed stale local metadata for missing worktree: $requestedTarget"
        } else {
            Write-Output "No worktree exists at '$requestedTarget'; nothing was removed."
        }
        return
    }

    $inventory = @(Get-RelevantWorktreeInventory $entries)
    $actualMatches = @($inventory | Where-Object { Test-PathEqual $_.Path $requestedTarget })
    
    # Case 10: FOREIGN_OR_UNMANAGED_DIRECTORY (not a known git worktree of any workspace repo)
    if ($actualMatches.Count -ne 1) {
        throw "Target '$requestedTarget' is an unmanaged/foreign directory under worktree root. Refusing automated removal to prevent data loss. Inspect status or clean up manually."
    }

    $actual = $actualMatches[0]
    if ($actual.IsPrimary) {
        throw "Refusing to remove the primary repository worktree '$requestedTarget'."
    }

    # Unregistered git worktree
    if ($null -eq $registered -and -not $Force) {
        throw "Target '$requestedTarget' is an unknown/unregistered worktree. Removal REFUSED; inspect status or provide -Force as explicit destructive confirmation."
    }
    if ($null -ne $registered -and (Get-EntryRepositoryName $registered) -ne $actual.Repository.Name) {
        throw "Registered repository '$((Get-EntryRepositoryName $registered))' does not match the Git worktree owner '$($actual.Repository.Name)'."
    }

    $mode = if ($null -ne $registered) {
        [string]$registered.mode
    } elseif ($actual.Detached) {
        "snapshot"
    } else {
        "writer"
    }
    $dirty = @(Get-DirtyLines $actual.Path $mode $registered)
    if ($dirty.Count -gt 0 -and -not $Force) {
        throw "Worktree '$requestedTarget' is dirty. Removal REFUSED; commit changes or provide -Force."
    }

    $unsafeReasons = @()
    if ($mode -eq "snapshot" -and $null -ne $registered) {
        $expectedSha = Get-EntryIntegrationSha $registered
        if (-not $actual.Detached -or $actual.Head -ine $expectedSha) {
            $unsafeReasons += "snapshot HEAD is no longer the recorded exact SHA"
        }
        if (-not (Test-SnapshotMarker $actual.Path $registered)) {
            $unsafeReasons += "snapshot marker is missing or changed"
        }
    }
    if ($mode -eq "writer" -and $null -ne $registered) {
        $unpushed = @(Get-UnpushedLines $actual.Path $registered)
        if ($unpushed.Count -gt 0) {
            $unsafeReasons += "unpushed commits or an unverified base are present"
        }
    }
    if ($unsafeReasons.Count -gt 0 -and -not $Force) {
        throw "Removal REFUSED for '$requestedTarget': $($unsafeReasons -join "; "). Provide -Force only after explicit destructive review."
    }

    if ($dirty.Count -gt 0 -or $unsafeReasons.Count -gt 0) {
        Write-Output "Destructive confirmation accepted via -Force for '$requestedTarget'."
    }

    # Check if other roles are sharing this snapshot (Case 7)
    if ($null -ne $registered) {
        $sharingRoles = @($entries | Where-Object {
            $null -ne $_.path -and
            (Test-PathEqual ([string]$_.path) $actual.Path) -and
            ([string]$_.role -ne [string]$registered.role)
        })

        if ($sharingRoles.Count -gt 0 -and -not $Force) {
            # Only deregister this role, do not physically remove the shared worktree
            Remove-RegistryEntry $worktreeRoot $requestedTarget ([string]$registered.role)
            Write-Output "Deregistered role '$($registered.role)' from shared worktree: $requestedTarget"
            return
        }
    }

    if ($mode -eq "snapshot" -and $dirty.Count -eq 0 -and (Test-Path -LiteralPath (Join-Path $actual.Path $SnapshotMarkerName))) {
        Remove-Item -LiteralPath (Join-Path $actual.Path $SnapshotMarkerName) -Force
    }
    $removeArguments = @("worktree", "remove")
    if ($Force) {
        $removeArguments += "--force"
    }
    $removeArguments += $actual.Path
    Invoke-Git $actual.Repository.FullPath $removeArguments | Out-Null
    if ($null -ne $registered) {
        Remove-RegistryEntry $worktreeRoot $requestedTarget ([string]$registered.role)
    }
    Write-Output "Removed worktree: $requestedTarget"
}

try {
    switch ($Action) {
        "create" { Invoke-CreateTaskWorktree }
        "snapshot" { Invoke-CreateSnapshotWorktree }
        "status" { Write-Status }
        "list" { Write-Status }
        "remove" { Invoke-RemoveWorktree }
        "cleanup" { Invoke-RemoveWorktree }
        "repair" {
            $worktreeRoot = Get-WorktreeRoot $Root
            Repair-RegistryEntries $worktreeRoot | Out-Null
            Write-Status
        }
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
