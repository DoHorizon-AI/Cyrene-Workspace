<#[
.SYNOPSIS
    Acceptance checks for Connector Package Spec v0.1.

.DESCRIPTION
    Keeps the static connector catalog tied to the versioned package schema
    while ensuring node-local installation state is not asserted by catalog
    metadata.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceDir = Split-Path -Parent $ScriptDir
$SchemaPath = Join-Path $WorkspaceDir "governance\connector-package-spec-v0.1.schema.json"
$CatalogPath = Join-Path $WorkspaceDir "governance\connectors.yaml"

$Passed = 0
$Failed = 0

function Assert-Rule([string]$RuleName, [scriptblock]$Check) {
    Write-Host -NoNewline "  $RuleName... "
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

if (-not (Test-Path -LiteralPath $SchemaPath)) {
    Write-Error "Package schema not found at: $SchemaPath"
    exit 1
}
if (-not (Test-Path -LiteralPath $CatalogPath)) {
    Write-Error "Connector catalog not found at: $CatalogPath"
    exit 1
}

$schema = Get-Content -LiteralPath $SchemaPath -Raw | ConvertFrom-Json
$rawCatalog = Get-Content -LiteralPath $CatalogPath -Raw

Assert-Rule "schema is versioned and has both record types" {
    $schema.'$id' -eq "https://cyrene.dev/schemas/connector-package-spec-v0.1.json" -and
        @($schema.oneOf).Count -eq 2 -and
        $schema.'$defs'.packageDescriptor.properties.record_type.const -eq "package_descriptor" -and
        $schema.'$defs'.installationRecord.properties.record_type.const -eq "installation_record"
}

Assert-Rule "package descriptor covers immutable identity and verification" {
    $required = @($schema.'$defs'.packageDescriptor.required)
    foreach ($field in @("package", "capability", "implementation", "dependencies", "configuration", "runtime", "compatibility", "integrity", "lifecycle", "provenance")) {
        if ($field -notin $required) { throw "Missing required package field: $field" }
    }
    $schema.'$defs'.packageDescriptor.properties.dependencies.properties.lock.oneOf.Count -eq 2
}

Assert-Rule "installation state is node-local, not catalog state" {
    $stateEnum = @($schema.'$defs'.installationRecord.properties.state.enum)
    "ACTIVE" -in $stateEnum -and
        $rawCatalog -notmatch '(?im)^\s+(installation_state|runtime_state|default_installation_status):' -and
        $rawCatalog -notmatch '(?im)^\s+[a-z_]+:\s*[''"]?(INSTALLED|RUNNING|CONFIGURED)[''"]?\s*$'
}

Assert-Rule "catalog references the package schema and OneBot remains a candidate" {
    $rawCatalog -match '(?m)^package_spec:' -and
        $rawCatalog -match 'connector-package-spec-v0\.1\.schema\.json' -and
        $rawCatalog -match '(?m)^\s+- connector_family: "onebot_v11"' -and
        $rawCatalog -match '(?m)^\s+publication_status: "CANDIDATE"'
}

Write-Host "Package spec validation: $Passed passed, $Failed failed"
if ($Failed -gt 0) { exit 1 }
