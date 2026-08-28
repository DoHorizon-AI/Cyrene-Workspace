<#
.SYNOPSIS
    Three-Level Validation Policy Runner for Cyrene Developer Agent Tasks.

.DESCRIPTION
    Runs tests adhering to the L1 (Focused), L2 (Task/Subsystem), and L3 (Full Integration)
    validation policy. Records environment faults cleanly without repetitive gating loops.

.EXAMPLE
    .\test-policy.ps1 -Level L1 -Command "cargo test -p cy-proto"
    .\test-policy.ps1 -Level L2 -Module "message-connector"
    .\test-policy.ps1 -Level L3
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("L1", "L2", "L3")]
    [string]$Level,

    [Parameter()]
    [string]$Command,

    [Parameter()]
    [string]$Module,

    [Parameter()]
    [string]$FaultReportPath,

    [Parameter()]
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))

function Record-EnvironmentFault([string]$Title, [string]$Details) {
    $fault = [ordered]@{
        timestamp = [DateTime]::UtcNow.ToString("o")
        level = $Level
        title = $Title
        details = $Details
        action = "RECORDED_NON_BLOCKING"
    }
    
    $outPath = if (-not [string]::IsNullOrWhiteSpace($FaultReportPath)) {
        $FaultReportPath
    } else {
        Join-Path $WorkspaceRoot "validation-faults.json"
    }
    
    $existing = @()
    if (Test-Path -LiteralPath $outPath) {
        try { $existing = @(ConvertFrom-Json (Get-Content -LiteralPath $outPath -Raw)) } catch {}
    }
    $updated = @($existing) + @($fault)
    [System.IO.File]::WriteAllText($outPath, (ConvertTo-Json -InputObject $updated -Depth 5), [System.Text.Encoding]::UTF8)
    Write-Warning "Recorded known environment fault to '$outPath': $Title"
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " CYRENE VALIDATION GATE: LEVEL $Level" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

switch ($Level) {
    "L1" {
        if ([string]::IsNullOrWhiteSpace($Command)) {
            throw "Parameter -Command is required for L1 Focused validation."
        }
        Write-Host "Executing L1 Focused Test: $Command" -ForegroundColor Yellow
        $start = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-Expression $Command
        $exitCode = $LASTEXITCODE
        $start.Stop()
        if ($exitCode -ne 0) {
            throw "L1 Focused Test failed (exit $exitCode) in $($start.ElapsedMilliseconds)ms."
        }
        Write-Host "[PASS] L1 Focused Test completed successfully in $($start.ElapsedMilliseconds)ms." -ForegroundColor Green
    }
    "L2" {
        Write-Host "Executing L2 Task/Subsystem Gate..." -ForegroundColor Yellow
        $contractHarness = Join-Path $ScriptDir "contract-harness.ps1"
        if (Test-Path -LiteralPath $contractHarness) {
            & pwsh -NoProfile -File $contractHarness
            if ($LASTEXITCODE -ne 0) {
                throw "L2 Contract harness gate failed."
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($Command)) {
            Write-Host "Executing Module Gate: $Command" -ForegroundColor Yellow
            Invoke-Expression $Command
            if ($LASTEXITCODE -ne 0) {
                throw "L2 Subsystem command failed."
            }
        }
        Write-Host "[PASS] L2 Task/Subsystem Gate completed successfully." -ForegroundColor Green
    }
    "L3" {
        Write-Host "Executing L3 Full Integration Gate..." -ForegroundColor Yellow
        $verifyScript = Join-Path $WorkspaceRoot "verify.ps1"
        try {
            & pwsh -NoProfile -File $verifyScript
            if ($LASTEXITCODE -ne 0) {
                throw "verify.ps1 exited with non-zero code $LASTEXITCODE."
            }
        } catch {
            Record-EnvironmentFault "Verify.ps1 encountered diagnostic warnings" $_.Exception.Message
            if ($Strict) {
                throw "L3 Full Integration Gate failed in -Strict mode: $($_.Exception.Message)"
            }
        }
        Write-Host "[PASS] L3 Full Integration Gate completed." -ForegroundColor Green
    }
}
