<#
.SYNOPSIS
    Bootstrap script for the Cyrene Multi-Repository Developer Workspace.
.DESCRIPTION
    Verifies topology, acquires missing repositories according to clone policy, restores Python
    environments with uv, checks developer toolchains, provisions IDE required plugins/datasources,
    and validates developer appliance readiness for the selected profile.
.EXAMPLE
    .\bootstrap.ps1 -Profile full
    .\bootstrap.ps1 -Profile platform -ProvisionIDE
#>

[CmdletBinding()]
param(
    [ValidateSet("full", "platform", "training")]
    [string]$Profile = "full",

    [switch]$ProvisionIDE,
    [switch]$ProvisionData,
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
Write-Host "`n[1/6] Verifying developer toolchains..." -ForegroundColor Yellow

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
    Write-Host "  [INFO] Java command not in PATH (toolchains configured via Gradle Daemon JVM 25)." -ForegroundColor Gray
}

# 2. Topology & Safe Repository Acquisition
Write-Host "`n[2/6] Checking repository topology & acquiring missing repositories for '$Profile'..." -ForegroundColor Yellow

$allRepos = @(
    @{ Name = "Cyrene-Platform"; Path = "../Cyrene-Platform"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Platform.git"; Policy = "public_zero_auth"; Profiles = @("full", "platform", "training") },
    @{ Name = "Cyrene-Plugins-Official"; Path = "../Cyrene-Plugins-Official"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git"; Policy = "public_zero_auth"; Profiles = @("full", "platform", "training") },
    @{ Name = "Cyrene-Reactor"; Path = "../Cyrene-Services/Cyrene-Reactor"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Reactor.git"; Policy = "public_zero_auth"; Profiles = @("full") },
    @{ Name = "Cyrene-Yield"; Path = "../Cyrene-Services/Cyrene-Yield"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Yield.git"; Policy = "public_zero_auth"; Profiles = @("full", "training") },
    @{ Name = "Cyrene-Exchange"; Path = "../Cyrene-Services/Cyrene-Exchange"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Exchange.git"; Policy = "public_zero_auth"; Profiles = @("full") },
    @{ Name = "Cyrene-Catalyst"; Path = "../Cyrene-Services/Cyrene-Catalyst"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Catalyst.git"; Policy = "public_zero_auth"; Profiles = @("full") },
    @{ Name = "Cyrene-Echo"; Path = "../Cyrene-Services/Cyrene-Echo"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Echo.git"; Policy = "public_zero_auth"; Profiles = @("full") },
    @{ Name = "Cyrene-Navigator"; Path = "../Cyrene-Services/Cyrene-Navigator"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Navigator.git"; Policy = "public_zero_auth"; Profiles = @("full") },
    @{ Name = "Cyrene-Client"; Path = "../Cyrene-Client"; Remote = "https://github.com/DoHorizon-AI/Cyrene-Client.git"; Slug = "DoHorizon-AI/Cyrene-Client"; Policy = "github_auth_required"; Profiles = @("full") }
)

$activeRepos = $allRepos | Where-Object { $_.Profiles -contains $Profile }
$missingRepos = [System.Collections.Generic.List[string]]::new()

foreach ($repo in $activeRepos) {
    $targetPath = Join-Path $ScriptDir $repo.Path
    if (Test-Path $targetPath) {
        Write-Host "  [FOUND] $($repo.Name) at $($repo.Path)" -ForegroundColor Green
    } else {
        Write-Host "  [ACQUIRING] $($repo.Name) (Policy: $($repo.Policy))..." -ForegroundColor Cyan
        if ($repo.Policy -eq "public_zero_auth") {
            git clone $repo.Remote $targetPath
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to clone public repository '$($repo.Name)'."
            }
            Write-Host "    [CLONED] $($repo.Name) successfully." -ForegroundColor Green
        } elseif ($repo.Policy -eq "github_auth_required") {
            $gh = Get-Command gh -ErrorAction SilentlyContinue
            if ($gh) {
                & gh auth status 2>$null | Out-Null
            }
            if ($gh -and $LASTEXITCODE -eq 0) {
                & gh repo clone $repo.Slug $targetPath
                if ($LASTEXITCODE -ne 0) {
                    throw "Failed to clone authenticated GitHub repository '$($repo.Name)'."
                }
                Write-Host "    [CLONED] $($repo.Name) successfully with GitHub authentication." -ForegroundColor Green
            } else {
                $missingRepos.Add($repo.Name)
                Write-Warning "    [AUTH_REQUIRED] Private repository '$($repo.Name)' requires an authenticated GitHub CLI. Run 'gh auth login', then rerun bootstrap."
            }
        } elseif ($repo.Policy -eq "external_auth_required") {
            $missingRepos.Add($repo.Name)
            Write-Warning "    [EXTERNAL_SOURCE] External private repository '$($repo.Name)' requires Azure DevOps authentication ($($repo.Remote))."
        }
    }
}

# 3. Python Environment Synchronization
if (-not $SkipPythonSync) {
    Write-Host "`n[3/6] Synchronizing Python environments with uv..." -ForegroundColor Yellow

    $allPythonTargets = @(
        @{ Repo = "Cyrene-Platform"; Path = "../Cyrene-Platform"; Extra = ""; Profiles = @("full", "platform", "training") },
        @{ Repo = "Cyrene-Reactor"; Path = "../Cyrene-Services/Cyrene-Reactor"; Extra = "--extra dev --extra pro"; Profiles = @("full") },
        @{ Repo = "Cyrene-Yield"; Path = "../Cyrene-Services/Cyrene-Yield"; Extra = "--extra dev"; Profiles = @("full", "training") },
        @{ Repo = "Cyrene-Exchange"; Path = "../Cyrene-Services/Cyrene-Exchange"; Extra = "--extra dev"; Profiles = @("full") }
    )

    $activePython = $allPythonTargets | Where-Object { $_.Profiles -contains $Profile }

    foreach ($target in $activePython) {
        $envPath = Join-Path $ScriptDir $target.Path
        if (Test-Path (Join-Path $envPath "pyproject.toml")) {
            Write-Host "  Syncing $($target.Repo)..." -ForegroundColor Cyan
            if ($target.Extra) {
                uv sync --locked --directory $envPath ($target.Extra -split " ")
            } else {
                uv sync --locked --directory $envPath
            }
        }
    }

    Write-Host "  Cyrene-Plugins-Official uses independent package/TCK pyproject.toml entrypoints; no root uv sync is performed." -ForegroundColor Gray
}

# 4. .NET Solution Restore
if (-not $SkipDotNetRestore) {
    Write-Host "`n[4/6] Restoring .NET solution(s)..." -ForegroundColor Yellow
    if ($Profile -eq "full") {
        $slnxPath = Join-Path $ScriptDir "Cyrene.Workspace.slnx"
        if (Test-Path $slnxPath) {
            dotnet restore $slnxPath -p:WarningsNotAsErrors=NU1902
            Write-Host "  [OK] Cyrene.Workspace.slnx restored successfully." -ForegroundColor Green
        }
    } else {
        Write-Host "  [INFO] No .NET solutions declared for profile '$Profile'." -ForegroundColor Gray
    }
}

# 5. Optional IDE Capability Provisioning
if ($ProvisionIDE) {
    Write-Host "`n[5/6] Provisioning IDE capabilities and checking required plugins..." -ForegroundColor Yellow
    
    $depsFile = Join-Path $ScriptDir ".idea/externalDependencies.xml"
    if (Test-Path $depsFile) {
        Write-Host "  [OK] Found declarative IDE dependencies at .idea/externalDependencies.xml" -ForegroundColor Green
        [xml]$depsXml = Get-Content $depsFile
        $plugins = $depsXml.project.component.plugin | ForEach-Object { $_.id }
        Write-Host "  Declared Required Plugins: $($plugins -join ', ')" -ForegroundColor Gray
    }

    # Detect IDE installations
    $hasIdea = (Get-Command idea -ErrorAction SilentlyContinue) -or 
               (Test-Path (Join-Path $env:LOCALAPPDATA "Programs\IntelliJ IDEA Ultimate\bin\idea64.exe"))
    $hasRider = (Get-Command rider -ErrorAction SilentlyContinue) -or 
                (Test-Path (Join-Path $env:LOCALAPPDATA "Programs\Rider\bin\rider64.exe"))

    if ($hasIdea) { Write-Host "  [DETECTED] IntelliJ IDEA installation ready." -ForegroundColor Green }
    if ($hasRider) { Write-Host "  [DETECTED] JetBrains Rider installation ready." -ForegroundColor Green }
} else {
    Write-Host "`n[5/6] IDE provisioning skipped (run with -ProvisionIDE to enable)." -ForegroundColor Gray
}

# 6. Optional Database & Dev Data Services Provisioning
if ($ProvisionData) {
    Write-Host "`n[6/6] Provisioning local database & dev services..." -ForegroundColor Yellow
    $dbManifest = Join-Path $ScriptDir "governance/databases.yaml"
    if (Test-Path $dbManifest) {
        Write-Host "  [OK] Loaded database manifest from governance/databases.yaml" -ForegroundColor Green
    }
    
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        $dockerRunning = (docker info 2>&1 | Select-String "Server Version") -ne $null
        if ($dockerRunning) {
            Write-Host "  [OK] Docker engine is active and ready for dev service containers." -ForegroundColor Green
        } else {
            Write-Host "  [INFO] Docker engine is installed but not running. Start Docker Desktop to launch local containers." -ForegroundColor Gray
        }
    } else {
        Write-Host "  [INFO] Docker command not in PATH. Local services can run via local PostgreSQL/Redis services." -ForegroundColor Gray
    }
} else {
    Write-Host "`n[6/6] Database provisioning skipped (run with -ProvisionData to enable)." -ForegroundColor Gray
}

# Summary
Write-Host "`n==================================================" -ForegroundColor Cyan
if ($missingRepos.Count -eq 0) {
    Write-Host " BOOTSTRAP COMPLETE FOR PROFILE: '$Profile'" -ForegroundColor Green
} else {
    Write-Host " BOOTSTRAP INCOMPLETE FOR PROFILE: '$Profile'" -ForegroundColor Yellow
    Write-Warning "Missing authenticated repositories: $($missingRepos -join ', ')"
}
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  To verify workspace: .\verify.ps1" -ForegroundColor Cyan
Write-Host "  To open IntelliJ IDEA: .\open.ps1 -Ide idea -Profile $Profile" -ForegroundColor Cyan
Write-Host "  To open JetBrains Rider: .\open.ps1 -Ide rider -Profile $Profile" -ForegroundColor Cyan
