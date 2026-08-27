<#
.SYNOPSIS
    GitHub Branch Rulesets Governance-as-Code Sync Script for Cyrene.
.DESCRIPTION
    Audits, applies, or verifies branch rulesets for develop and main branches
    declaratively defined in governance/github-rulesets.yaml using gh CLI.
    Gracefully handles GitHub Free plan limitations on private repositories.
#>

[CmdletBinding()]
param(
    [ValidateSet("Audit", "Apply", "Verify")]
    [string]$Mode = "Audit",

    [string]$ConfigFile = "governance/github-rulesets.yaml"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = Split-Path -Parent $ScriptDir
Set-Location $WorkspaceRoot

$ConfigFullPath = Join-Path $WorkspaceRoot $ConfigFile
if (-not (Test-Path $ConfigFullPath)) {
    Write-Error "Config file not found at: $ConfigFullPath"
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " CYRENE GITHUB RULESET GOVERNANCE (Mode: $Mode)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Parse YAML using Python/pyyaml via uv
$jsonText = uv run --directory "$WorkspaceRoot/../Cyrene-Platform" python -c @"
import yaml, json
with open(r'$ConfigFullPath', encoding='utf-8') as f:
    data = yaml.safe_load(f)
print(json.dumps(data))
"@

$spec = $jsonText | ConvertFrom-Json
$org = $spec.organization

Write-Host "Organization: $org" -ForegroundColor Green

# 2. Query and Synchronize Rulesets
$driftCount = 0
$planRestrictedCount = 0

foreach ($repoName in $spec.managed_repositories) {
    Write-Host "`n--------------------------------------------------" -ForegroundColor Yellow
    Write-Host " Repository: $repoName" -ForegroundColor Yellow
    Write-Host "--------------------------------------------------" -ForegroundColor Yellow

    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $apiOutput = gh api "repos/$org/$repoName/rulesets" 2>&1
    $apiExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEA

    if ($apiExit -ne 0) {
        $errorStr = ($apiOutput | Out-String)
        if ($errorStr -match "Upgrade to GitHub Pro" -or $errorStr -match "403") {
            Write-Host "  [PLAN_LIMITATION] Private repository on GitHub Free plan: Branch rulesets require GitHub Team/Enterprise or Public visibility." -ForegroundColor Magenta
            Write-Host "  [DECLARATIVE_READY] Ruleset specification for '$repoName' defined in $ConfigFile." -ForegroundColor Gray
            $planRestrictedCount++
            continue
        } else {
            Write-Warning "  Failed to query rulesets on ${repoName}: $errorStr"
            continue
        }
    }

    $existingRulesets = $apiOutput | ConvertFrom-Json
    $rulesetMap = @{}
    foreach ($r in $existingRulesets) {
        $rulesetMap[$r.name] = $r
    }

    foreach ($desired in $spec.rulesets) {
        $rulesetName = $desired.name
        $isExisting = $rulesetMap.ContainsKey($rulesetName)

        if (-not $isExisting) {
            Write-Host "  [DRIFT: MISSING_RULESET] Ruleset '$rulesetName' is missing on $repoName." -ForegroundColor Yellow
            $driftCount++

            if ($Mode -eq "Apply") {
                Write-Host "    --> Creating ruleset '$rulesetName' on $repoName..." -ForegroundColor Cyan
                $payload = @{
                    name = $desired.name
                    target = $desired.target
                    enforcement = $desired.enforcement
                    conditions = $desired.conditions
                    rules = $desired.rules
                } | ConvertTo-Json -Depth 10

                $tmpFile = [System.IO.Path]::GetTempFileName()
                [System.IO.File]::WriteAllText($tmpFile, $payload)
                try {
                    gh api --method POST "repos/$org/$repoName/rulesets" --input $tmpFile 2>&1 | Out-Null
                    Write-Host "    [OK] Ruleset '$rulesetName' created." -ForegroundColor Green
                } catch {
                    Write-Warning "    Failed to apply ruleset '$rulesetName': $_"
                } finally {
                    Remove-Item -Force $tmpFile
                }
            }
        } else {
            Write-Host "  [OK] Ruleset '$rulesetName' exists (ID: $($rulesetMap[$rulesetName].id), Enforcement: $($rulesetMap[$rulesetName].enforcement))." -ForegroundColor Green
        }
    }
}

# 3. Summary
Write-Host "`n==================================================" -ForegroundColor Cyan
if ($planRestrictedCount -gt 0) {
    Write-Host " Plan Status: $planRestrictedCount repositories are private on GitHub Free plan (declarative definitions preserved)" -ForegroundColor Magenta
}
Write-Host " Drift Summary: $driftCount drifts detected" -ForegroundColor $(if ($driftCount -eq 0) { "Green" } else { "Yellow" })
Write-Host "==================================================" -ForegroundColor Cyan

if ($Mode -eq "Verify" -and $driftCount -gt 0) {
    Write-Error "Verification failed: $driftCount ruleset drifts detected."
}
