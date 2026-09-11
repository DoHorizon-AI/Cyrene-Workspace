<#
.SYNOPSIS
    Opens the Cyrene Developer Workspace in IntelliJ IDEA or JetBrains Rider.
.DESCRIPTION
    Automatically detects JetBrains IDE installations (including JetBrains Toolbox and standard paths)
    and opens the appropriate multi-project workspace or focused solution according to the profile.
.EXAMPLE
    .\open.ps1 -Ide idea -Profile full
    .\open.ps1 -Ide rider -Profile platform
#>

[CmdletBinding()]
param(
    [ValidateSet("idea", "rider")]
    [string]$Ide = "idea",

    [ValidateSet("full", "platform", "training")]
    [string]$Profile = "full"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Executable {
    param([string[]]$Candidates, [string]$CommandName)

    # 1. Check PATH
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    # 2. Check candidate filesystem paths
    foreach ($path in $Candidates) {
        if (Test-Path $path) {
            return $path
        }
    }

    # 3. Check JetBrains Toolbox apps directory dynamically
    $toolboxRoot = Join-Path $env:LOCALAPPDATA "JetBrains\Toolbox\apps"
    if (Test-Path $toolboxRoot) {
        $matches = Get-ChildItem -Path $toolboxRoot -Recurse -Filter "$CommandName*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($matches) {
            return $matches.FullName
        }
    }

    return $null
}

if ($Ide -eq "idea") {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\IntelliJ IDEA Ultimate\bin\idea64.exe"),
        (Join-Path $env:ProgramFiles "JetBrains\IntelliJ IDEA Ultimate\bin\idea64.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "JetBrains\IntelliJ IDEA Ultimate\bin\idea64.exe")
    )
    $exe = Find-Executable -Candidates $candidates -CommandName "idea"

    if (-not $exe) {
        Write-Error "IntelliJ IDEA installation not found. Please ensure IntelliJ IDEA is installed or in PATH."
    }

    Write-Host "Opening IntelliJ IDEA Multi-Project Workspace ($ScriptDir)..." -ForegroundColor Cyan
    Start-Process -FilePath $exe -ArgumentList "`"$ScriptDir`""
} elseif ($Ide -eq "rider") {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Rider\bin\rider64.exe"),
        (Join-Path $env:ProgramFiles "JetBrains\JetBrains Rider\bin\rider64.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "JetBrains\JetBrains Rider\bin\rider64.exe")
    )
    $exe = Find-Executable -Candidates $candidates -CommandName "rider"

    if (-not $exe) {
        Write-Error "JetBrains Rider installation not found. Please ensure JetBrains Rider is installed or in PATH."
    }

    $targetSolution = Join-Path $ScriptDir "Cyrene.Workspace.slnx"

    Write-Host "Opening JetBrains Rider Solution ($targetSolution)..." -ForegroundColor Cyan
    Start-Process -FilePath $exe -ArgumentList "`"$targetSolution`""
}
