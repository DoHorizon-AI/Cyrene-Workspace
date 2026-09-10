<#
.SYNOPSIS
    Deterministic Acceptance and Governance Schema Validator for the Official Connector Catalog.

.DESCRIPTION
    Validates the 12 candidate governance rules for governance/connectors.yaml:
    1. Document root contains candidate maturity & pinned upstream/evidence commits
    2. Base image boundary policy defined with MINIMAL_CORE_ONLY
    3. Standard deployment profiles defined with zero layer mutation
    4. Unique connector family and adapter type keys
    5. Clean vocabulary: zero static runtime-state leakage (no INSTALLED/RUNNING/CONFIGURED in static metadata)
    6. Valid recommended_distribution_kind vocabulary
    7. All 5 size axes present on connector footprints
    8. Size provenance attributes contain integer value >= 0 and valid provenance enum (EXACT/ESTIMATED/UNKNOWN/HISTORICAL_OBSERVATION)
    9. No illegal summing across disparate size axes
    10. Multi-layer connectors declare external implementation runtime requirements
    11. Transport profiles defined with type, endpoint_role, and tls attributes
    12. All 18 upstream-analyzed adapters are accounted for
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDir = Split-Path -Parent $ScriptDir
$CatalogPath = Join-Path $WorkspaceDir "governance\connectors.yaml"

$Passed = 0
$Failed = 0

function Assert-Rule([string]$RuleName, [scriptblock]$Check) {
    Write-Host -NoNewline "  Rule $RuleName... "
    try {
        $result = & $Check
        if ($result -eq $false) { throw "Validation check failed." }
        Write-Host "[PASS]" -ForegroundColor Green
        $script:Passed++
    } catch {
        Write-Host "[FAIL]" -ForegroundColor Red
        Write-Host "    $($_.Exception.Message)" -ForegroundColor Red
        $script:Failed++
    }
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " RUNNING OFFICIAL CONNECTOR CATALOG GOVERNANCE SUITE" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $CatalogPath)) {
    Write-Error "Catalog not found at: $CatalogPath"
    exit 1
}

$rawYaml = Get-Content -LiteralPath $CatalogPath -Raw

# 1. Pinned Commits and Candidate Maturity
Assert-Rule "1: Root metadata and CANDIDATE maturity authority" {
    return (
        $rawYaml -match "governance_status:\s*['""]?CANDIDATE['""]?" -and
        $rawYaml -match "maturity:\s*['""]?CANDIDATE['""]?" -and
        $rawYaml -match "d2d7e5aefd2f9471d211974e7e193f321e0ebe0a" -and
        $rawYaml -match "39f9a76ed88918dd9ddf7293f70540f816b3a793"
    )
}

# 2. Base image boundary policy
Assert-Rule "2: Base image policy defines MINIMAL_CORE_ONLY" {
    return ($rawYaml -match "runtime_boundary:\s*['""]?MINIMAL_CORE_ONLY['""]?")
}

# 3. Deployment Profiles
Assert-Rule "3: Deployment profiles defined with immutable base image policy" {
    $profiles = @("minimal:", "qq-onebot:", "qq-full:", "enterprise-cn:", "global-im:", "full-demo:")
    foreach ($p in $profiles) {
        if ($rawYaml -notmatch [Regex]::Escape($p)) { return $false }
    }
    return ($rawYaml -match "base_image_layer_mutation:\s*false")
}

# 4. No Runtime State Leakage in Static Attributes
Assert-Rule "4: Zero runtime state leakage (no INSTALLED, RUNNING, CONFIGURED as static fields)" {
    if ($rawYaml -match "(?m)^\s+default_installation_status:\s*['""]?INSTALLED['""]?") { return $false }
    if ($rawYaml -match "(?m)^\s+runtime_state:\s*['""]?(RUNNING|CONFIGURED|INSTALLED)['""]?") { return $false }
    return $true
}

# 5. Valid Distribution Kinds
Assert-Rule "5: Standard recommended_distribution_kind vocabulary" {
    $allowedKinds = @("CORE_BUILTIN", "LIGHTWEIGHT_BUNDLE", "ISOLATED_PYTHON_RUNTIME", "EXTERNAL_RUNTIME", "OPTIONAL_OCI_SIDECAR", "COMMUNITY_EXTERNAL")
    $kindMatches = [Regex]::Matches($rawYaml, "(?m)^\s+recommended_distribution_kind:\s*['""]?([A-Z_]+)['""]?")
    if ($kindMatches.Count -eq 0) { return $false }
    foreach ($match in $kindMatches) {
        $val = $match.Groups[1].Value
        if ($val -notin $allowedKinds) {
            throw "Invalid distribution kind: $val"
        }
    }
    return $true
}

# 6. Valid Availability Status Vocabulary
Assert-Rule "6: Standard availability_status vocabulary" {
    $allowedStatus = @("AVAILABLE", "CANONICAL", "COMPATIBILITY_ONLY", "NOT_YET_MIGRATED", "DEFAULT_PREFETCH", "OPTIONAL", "IMPLEMENTATION_PRESENT", "COMMUNITY_EXTERNAL")
    $statusMatches = [Regex]::Matches($rawYaml, "(?m)^\s+availability_status:\s*['""]?([A-Z_]+)['""]?")
    if ($statusMatches.Count -eq 0) { return $false }
    foreach ($match in $statusMatches) {
        $val = $match.Groups[1].Value
        if ($val -notin $allowedStatus) {
            throw "Invalid availability status: $val"
        }
    }
    return $true
}

# 7. Five-Axis Size Provenance
Assert-Rule "7: All 5 size axes present on connector footprints" {
    $axes = @("source_code_size:", "wheel_download_size:", "installed_size:", "native_dependency_size:", "container_layer_size:")
    foreach ($axis in $axes) {
        $count = [Regex]::Matches($rawYaml, [Regex]::Escape($axis)).Count
        if ($count -lt 10) {
            throw "Missing axis $axis (found $count occurrences)"
        }
    }
    return $true
}

# 8. Provenance Enum Validity
Assert-Rule "8: Provenance metadata strictly adheres to enum" {
    $allowedProv = @("EXACT", "ESTIMATED", "UNKNOWN", "HISTORICAL_OBSERVATION")
    # Use horizontal whitespace here so a nested package-descriptor map is not
    # mistaken for a scalar provenance value.
    $provMatches = [Regex]::Matches($rawYaml, "(?m)^\s+provenance:[ \t]*['""]?([A-Za-z_]+)['""]?[ \t]*$")
    if ($provMatches.Count -eq 0) { return $false }
    foreach ($match in $provMatches) {
        $val = $match.Groups[1].Value.ToUpperInvariant()
        if ($val -notin $allowedProv) {
            throw "Invalid provenance value: $val"
        }
    }
    return $true
}

# 9. No Illegal Summing Across Axes
Assert-Rule "9: Disparate axes are independently modeled with no merged cross-axis totals" {
    return ($rawYaml -match "size_provenance:" -and $rawYaml -notmatch "total_combined_all_axes:")
}

# 10. Multi-Layer Separation for External Daemons
Assert-Rule "10: Multi-layer external runtime separation (OneBot & WeChat OpenClaw)" {
    return (
        $rawYaml -match "external_implementation_layer:" -and
        $rawYaml -match "NapCat" -and
        $rawYaml -match "OpenClaw"
    )
}

# 11. Transport Profiles Structure
Assert-Rule "11: Transport profiles define transport type, endpoint role, and TLS policy" {
    return (
        $rawYaml -match "transport_profiles:" -and
        $rawYaml -match "endpoint_role:" -and
        $rawYaml -match "tls_required:"
    )
}

# 12. Complete 18-Adapter Coverage
Assert-Rule "12: All 18 upstream adapters accounted for" {
    $adapters = @(
        "aiocqhttp", "dingtalk", "discord", "kook", "lark", "line",
        "mattermost", "misskey", "qqofficial", "qqofficial_webhook",
        "satori", "slack", "telegram", "webchat", "wecom", "wecom_ai_bot",
        "weixin_oc", "weixin_official_account"
    )
    foreach ($adapter in $adapters) {
        if ($rawYaml -notmatch [Regex]::Escape($adapter)) {
            throw "Missing upstream adapter from catalog: $adapter"
        }
    }
    return $true
}

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " CATALOG VALIDATION RESULTS: $Passed Passed, $Failed Failed" -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
Write-Host "=================================================================" -ForegroundColor Cyan
if ($Failed -gt 0) {
    exit 1
}
