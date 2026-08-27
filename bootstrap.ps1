<#
.SYNOPSIS
    Bootstrap script for the Cyrene Multi-Repository Developer Workspace.
.DESCRIPTION
    Verifies topology, restores Python environments, checks toolchains (.NET, Rust, Java/Gradle),
    and validates IDE readiness for the selected developer profile without modifying uncommitted changes.
#>

[CmdletBinding()]
param(
    [ValidateSet("full", "astrbot", "platform", "training")]
    [string]$Profile = "full",

    [switch]$SkipPythonSync,
    [switch]$SkipDotNetRestore
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " CYRENE WORKSPACE BOOTSTRAP (Profile: $Profile)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Toolchain Verification
Write-Host "`n[1/5] Verifying developer toolchains..." -ForegroundColor Yellow

# .NET SDK
try {
    $dotnetVersion = dotnet --version
    Write-Host "  [OK] .NET SDK: $dotnetVersion" -ForegroundColor Green
} catch {
    Write-Warning "  [WARN] .NET SDK not found. Install .NET 10 SDK."
}

# Rust / Cargo
try {
    $cargoVersion = cargo --version
    Write-Host "  [OK] Rust / Cargo: $cargoVersion" -ForegroundColor Green
} catch {
    Write-Warning "  [WARN] Cargo not found. Install Rust toolchain."
}

# uv (Python package manager)
try {
    $uvVersion = uv --version
    Write-Host "  [OK] uv: $uvVersion" -ForegroundColor Green
} catch {
    Write-Warning "  [WARN] uv not found. Install uv (https://docs.astral.sh/uv/)."
}

# Java / JDK
try {
    $javaVersion = java -version 2>&1 | Select-Object -First 1
    Write-Host "  [OK] Java: $javaVersion" -ForegroundColor Green
} catch {
    Write-Host "  [INFO] Java command not in PATH (toolchains configured via Gradle)." -ForegroundColor Gray
}

# 2. Topology Verification by Profile
Write-Host "`n[2/5] Checking repository topology for profile '$Profile'..." -ForegroundColor Yellow

$allRepos = @(
    @{ Name = "Cyrene-Platform"; Path = "../Cyrene-Platform"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Platform.git"; Profiles = @("full", "astrbot", "platform", "training") },
    @{ Name = "Cyrene-Plugins"; Path = "../plugins"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git"; Profiles = @("full", "astrbot", "platform", "training") },
    @{ Name = "cyrene-astrbot-rev"; Path = "../services/cyrene-astrbot-rev"; Remote = "https://github.com/DoHorizon-AI/Astrbot-Rev.git"; Profiles = @("full", "astrbot") },
    @{ Name = "cyrene-dh-system-internal"; Path = "../services/cyrene-dh-system-internal"; Remote = "https://dohorizon@dev.azure.com/dohorizon/Cyrene/_git/DH-System-Internal"; Profiles = @("full") },
    @{ Name = "cyrene-reactor"; Path = "../services/cyrene-reactor"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Reactor.git"; Profiles = @("full") },
    @{ Name = "Cyrene-Yield"; Path = "../services/Cyrene-Yield"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Yield.git"; Profiles = @("full", "training") },
    @{ Name = "cyrene-exchange"; Path = "../services/cyrene-exchange"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Exchange.git"; Profiles = @("full", "astrbot") },
    @{ Name = "cyrene-catalyst"; Path = "../services/cyrene-catalyst"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Catalyst.git"; Profiles = @("full") },
    @{ Name = "cyrene-echo"; Path = "../services/cyrene-echo"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Echo.git"; Profiles = @("full") },
    @{ Name = "cyrene-navigator"; Path = "../services/cyrene-navigator"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Navigator.git"; Profiles = @("full") }
)

$activeRepos = $allRepos | Where-Object { $_.Profiles -contains $Profile }

foreach ($repo in $activeRepos) {
    $targetPath = Join-Path $ScriptDir $repo.Path
    if (Test-Path $targetPath) {
        Write-Host "  [FOUND] $($repo.Name) at $($repo.Path)" -ForegroundColor Green
    } else {
        Write-Host "  [MISSING] $($repo.Name) at $($repo.Path)" -ForegroundColor Yellow
        if ($repo.Remote) {
            Write-Host "    Cloning from $($repo.Remote)..." -ForegroundColor Cyan
            git clone $repo.Remote $targetPath
        }
    }
}

# 3. Python Environment Synchronization
if (-not $SkipPythonSync) {
    Write-Host "`n[3/5] Synchronizing Python environments with uv..." -ForegroundColor Yellow

    $allPythonTargets = @(
        @{ Repo = "Cyrene-Platform"; Path = "../Cyrene-Platform"; Extra = ""; Profiles = @("full", "astrbot", "platform", "training") },
        @{ Repo = "plugins"; Path = "../plugins"; Extra = ""; Profiles = @("full", "astrbot", "platform", "training") },
        @{ Repo = "cyrene-reactor"; Path = "../services/cyrene-reactor"; Extra = "--extra dev --extra pro"; Profiles = @("full") },
        @{ Repo = "Cyrene-Yield"; Path = "../services/Cyrene-Yield"; Extra = "--extra dev"; Profiles = @("full", "training") },
        @{ Repo = "cyrene-exchange"; Path = "../services/cyrene-exchange"; Extra = "--extra dev"; Profiles = @("full", "astrbot") },
        @{ Repo = "cyrene-astrbot-rev/capability_worker"; Path = "../services/cyrene-astrbot-rev/python/capability_worker"; Extra = ""; Profiles = @("full", "astrbot") }
    )

    $activePython = $allPythonTargets | Where-Object { $_.Profiles -contains $Profile }

    foreach ($target in $activePython) {
        $envPath = Join-Path $ScriptDir $target.Path
        if (Test-Path (Join-Path $envPath "pyproject.toml")) {
            Write-Host "  Syncing $($target.Repo)..." -ForegroundColor Cyan
            if ($target.Extra) {
                uv sync --directory $envPath ($target.Extra -split " ")
            } else {
                uv sync --directory $envPath
            }
        }
    }
}

# 4. .NET Solution Restore
if (-not $SkipDotNetRestore) {
    Write-Host "`n[4/5] Restoring .NET solution(s)..." -ForegroundColor Yellow
    if ($Profile -eq "astrbot") {
        $slnxPath = Join-Path $ScriptDir "solutions/Cyrene.AstrBot.Integration.slnx"
        if (Test-Path $slnxPath) {
            dotnet restore $slnxPath
            Write-Host "  [OK] Cyrene.AstrBot.Integration.slnx restored successfully." -ForegroundColor Green
        }
    } elseif ($Profile -eq "full") {
        $slnxPath = Join-Path $ScriptDir "Cyrene.Workspace.slnx"
        if (Test-Path $slnxPath) {
            dotnet restore $slnxPath
            Write-Host "  [OK] Cyrene.Workspace.slnx restored successfully." -ForegroundColor Green
        }
    } else {
        Write-Host "  [INFO] No .NET solutions declared for profile '$Profile'." -ForegroundColor Gray
    }
}

# 5. Summary
Write-Host "`n[5/5] Bootstrap complete for profile '$Profile'!" -ForegroundColor Green
Write-Host "To verify the workspace, run: .\verify.ps1" -ForegroundColor Cyan
Write-Host "To open in IntelliJ IDEA: open the 'Cyrene-Workspace' folder." -ForegroundColor Cyan
if ($Profile -eq "astrbot") {
    Write-Host "To open in JetBrains Rider: open 'Cyrene-Workspace/solutions/Cyrene.AstrBot.Integration.slnx'." -ForegroundColor Cyan
} else {
    Write-Host "To open in JetBrains Rider: open 'Cyrene-Workspace/Cyrene.Workspace.slnx'." -ForegroundColor Cyan
}
