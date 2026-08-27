<#
.SYNOPSIS
    GitHub Access Governance-as-Code Sync Script for Cyrene.
.DESCRIPTION
    Audits, applies, or verifies team memberships and repository permissions
    declaratively defined in governance/github-access.yaml using gh CLI.
#>

[CmdletBinding()]
param(
    [ValidateSet("Audit", "Apply", "Verify")]
    [string]$Mode = "Audit",

    [string]$ConfigFile = "governance/github-access.yaml"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = Split-Path -Parent $ScriptDir
Set-Location $WorkspaceRoot

$ConfigFullPath = Join-Path $WorkspaceRoot $ConfigFile
if (-not (Test-Path $ConfigFullPath)) {
    Write-Error "Config file not found at: $ConfigFullPath"
}

# 1. Parse YAML using Python/pyyaml via uv
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " CYRENE GITHUB ACCESS GOVERNANCE (Mode: $Mode)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$jsonText = uv run --directory "$WorkspaceRoot/../Cyrene-Platform" python -c @"
import yaml, json, sys
with open(r'$ConfigFullPath', encoding='utf-8') as f:
    data = yaml.safe_load(f)
print(json.dumps(data))
"@

$spec = $jsonText | ConvertFrom-Json
$org = $spec.organization

Write-Host "Organization: $org" -ForegroundColor Green

# 2. Query Current GitHub State
Write-Host "`n[1/3] Querying current GitHub state for $org..." -ForegroundColor Yellow

$remoteTeams = gh api "orgs/$org/teams?per_page=100" | ConvertFrom-Json
$remoteTeamDict = @{}
foreach ($t in $remoteTeams) {
    $remoteTeamDict[$t.slug] = $t
}

# Collect current team members and team repos
$currentTeamMembers = @{}
$currentTeamRepos = @{}

foreach ($teamSlug in $remoteTeamDict.Keys) {
    # Members
    $members = gh api "orgs/$org/teams/$teamSlug/members?per_page=100" 2>$null | ConvertFrom-Json
    $currentTeamMembers[$teamSlug] = @($members | ForEach-Object { $_.login })

    # Repos
    $repos = gh api "orgs/$org/teams/$teamSlug/repos?per_page=100" 2>$null | ConvertFrom-Json
    $repoPermMap = @{}
    foreach ($r in $repos) {
        $level = "pull"
        if ($r.permissions.admin) { $level = "admin" }
        elseif ($r.permissions.maintain) { $level = "maintain" }
        elseif ($r.permissions.push) { $level = "push" }
        elseif ($r.permissions.triage) { $level = "triage" }
        $repoPermMap[$r.name] = $level
    }
    $currentTeamRepos[$teamSlug] = $repoPermMap
}

# 3. Drift Calculation
Write-Host "`n[2/3] Analyzing access drift against $ConfigFile..." -ForegroundColor Yellow

$driftCount = 0

# A. Team Membership Drift
foreach ($team in $spec.teams) {
    $slug = $team.slug
    if (-not $remoteTeamDict.ContainsKey($slug)) {
        Write-Host "  [DRIFT: MISSING_TEAM] Team '$slug' does not exist on GitHub." -ForegroundColor Red
        $driftCount++
        if ($Mode -eq "Apply") {
            Write-Host "    --> Creating team '$slug'..." -ForegroundColor Cyan
            $tmpFile = [System.IO.Path]::GetTempFileName()
            @{ name = $team.name; privacy = $team.privacy; description = $team.description } | ConvertTo-Json | Set-Content -Path $tmpFile -Encoding utf8
            try {
                gh api --method POST "orgs/$org/teams" --input $tmpFile 2>&1 | Out-Null
            } finally {
                Remove-Item -Force $tmpFile
            }
        }
    }

    $existingMembers = if ($currentTeamMembers.ContainsKey($slug)) { $currentTeamMembers[$slug] } else { @() }
    $desiredMembers = @($team.members | ForEach-Object { $_.login })

    foreach ($desired in $team.members) {
        if ($existingMembers -notcontains $desired.login) {
            Write-Host "  [DRIFT: MISSING_MEMBER] Team '$slug' is missing member '$($desired.login)' (Role: $($desired.role))." -ForegroundColor Yellow
            $driftCount++
            if ($Mode -eq "Apply") {
                Write-Host "    --> Adding $($desired.login) to team $slug..." -ForegroundColor Cyan
                gh api --method PUT "orgs/$org/teams/$slug/memberships/$($desired.login)" -f role="$($desired.role)" 2>&1 | Out-Null
            }
        }
    }
}

# B. Repository Team Permissions Drift
foreach ($repo in $spec.repositories) {
    $repoName = $repo.name
    $desiredTeamPerms = $repo.team_permissions

    foreach ($prop in $desiredTeamPerms.PSObject.Properties) {
        $teamSlug = $prop.Name
        $desiredPerm = $prop.Value

        $currentPerm = $null
        if ($currentTeamRepos.ContainsKey($teamSlug) -and $currentTeamRepos[$teamSlug].ContainsKey($repoName)) {
            $currentPerm = $currentTeamRepos[$teamSlug][$repoName]
        }

        if ($currentPerm -ne $desiredPerm) {
            Write-Host "  [DRIFT: REPO_PERMISSION] Repo '$repoName' for team '$teamSlug': Current='$currentPerm', Desired='$desiredPerm'" -ForegroundColor Yellow
            $driftCount++
            if ($Mode -eq "Apply") {
                Write-Host "    --> Applying permission '$desiredPerm' on $repoName for team $teamSlug..." -ForegroundColor Cyan
                gh api --method PUT "orgs/$org/teams/$teamSlug/repos/$org/$repoName" -f permission="$desiredPerm" 2>&1 | Out-Null
            }
        }
    }
}

# 4. Summary & Verification Gate
Write-Host "`n[3/3] Drift Summary" -ForegroundColor Yellow
if ($driftCount -eq 0) {
    Write-Host "  [IN SYNC] All team memberships and repository permissions match declarative governance!" -ForegroundColor Green
} else {
    Write-Host "  Total Drifts: $driftCount" -ForegroundColor $(if ($Mode -eq "Apply") { "Cyan" } else { "Yellow" })
}

if ($Mode -eq "Verify" -and $driftCount -gt 0) {
    Write-Error "Verification failed: $driftCount access drifts detected."
}
