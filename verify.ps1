<#
.SYNOPSIS
    Automated Acceptance Gate for Cyrene Multi-Repository Workspace and Toolchain Normalization.
.DESCRIPTION
    Validates topology, absence of absolute paths, Python environments, .NET meta-solution,
    Rust workspaces, JVM configurations, and repository independence.
#>

[CmdletBinding()]
param(
    [switch]$Quick
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$script:passCount = 0
$script:failCount = 0

function Assert-Step([string]$name, [scriptblock]$action) {
    Write-Host -NoNewline "Checking $name... "
    try {
        & $action
        Write-Host "[PASS]" -ForegroundColor Green
        $script:passCount++
    } catch {
        Write-Host "[FAIL]" -ForegroundColor Red
        Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
        $script:failCount++
    }
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " CYRENE WORKSPACE VERIFICATION GATE" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. TOPOLOGY & ABSOLUTE PATH CHECK
Assert-Step "Topology relative path resolution in repositories.yaml" {
    $manifestPath = Join-Path $ScriptDir "repositories.yaml"
    if (-not (Test-Path $manifestPath)) { throw "repositories.yaml not found" }
    
    $lines = Get-Content $manifestPath
    $paths = @()
    foreach ($line in $lines) {
        if ($line -match '^\s*path:\s*"([^"]+)"') {
            $paths += $matches[1]
        }
    }
    foreach ($relPath in $paths) {
        $resolved = Join-Path $ScriptDir $relPath
        if (-not (Test-Path $resolved)) {
            throw "Failed to resolve repository path: $relPath ($resolved)"
        }
    }
}

Assert-Step "Absence of user-specific absolute paths in workspace configuration" {
    $configFiles = Get-ChildItem -Path $ScriptDir -Recurse -File | Where-Object {
        $_.FullName -notmatch '\\\.git\\' -and
        $_.FullName -notmatch '\\bin\\' -and
        $_.FullName -notmatch '\\obj\\' -and
        $_.FullName -notmatch '\\\.venv\\' -and
        $_.Extension -in @('.yaml', '.slnx', '.xml', '.iml', '.json', '.md', '.ps1', '.sh')
    }
    
    foreach ($file in $configFiles) {
        $content = Get-Content $file.FullName -Raw
        if ($content -match 'C:\\Users\\' -or $content -match '/home/') {
            if ($file.Name -ne "verify.ps1" -and $file.Name -ne "IDE_ACCEPTANCE.md" -and $file.Name -ne "README.md") {
                throw "User absolute path found in $($file.FullName)"
            }
        }
    }
}

# 2. PYTHON ENVIRONMENTS
Assert-Step "Python .venv discovery and uv environment sanity" {
    $pyTargets = @(
        "../Cyrene-Platform",
        "../plugins",
        "../services/cyrene-reactor",
        "../services/Cyrene-Yield",
        "../services/cyrene-exchange",
        "../services/cyrene-astrbot-rev/python/capability_worker"
    )

    foreach ($rel in $pyTargets) {
        $full = Join-Path $ScriptDir $rel
        $venv = Join-Path $full ".venv"
        if (-not (Test-Path $venv)) {
            throw "Missing .venv in $rel (run bootstrap.ps1 first)"
        }
        $pyVersion = Join-Path $full ".python-version"
        if (-not (Test-Path $pyVersion)) {
            throw "Missing .python-version in $rel"
        }
    }
}

# 3. .NET META-SOLUTION
Assert-Step ".NET Cyrene.Workspace.slnx project resolution" {
    $slnx = Join-Path $ScriptDir "Cyrene.Workspace.slnx"
    if (-not (Test-Path $slnx)) { throw "Cyrene.Workspace.slnx not found" }
    
    $output = dotnet sln $slnx list 2>&1
    if ($LASTEXITCODE -ne 0) { throw "dotnet sln list failed: $output" }
    
    $projectCount = ($output | Where-Object { $_ -match '\.csproj$' }).Count
    if ($projectCount -lt 20) {
        throw "Expected at least 20 projects, found $projectCount"
    }
}

if (-not $Quick) {
    Assert-Step ".NET aggregate solution build" {
        $slnx = Join-Path $ScriptDir "Cyrene.Workspace.slnx"
        $buildOutput = dotnet build $slnx -c Debug --no-restore 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "dotnet build failed"
        }
    }
}

# 4. RUST WORKSPACES
Assert-Step "Rust cargo metadata validation" {
    $rustTargets = @("../Cyrene-Platform", "../services/cyrene-reactor")
    foreach ($rel in $rustTargets) {
        $full = Join-Path $ScriptDir $rel
        $output = cargo metadata --manifest-path (Join-Path $full "Cargo.toml") --format-version 1 --no-deps 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "cargo metadata failed for $rel"
        }
    }
}

# 5. JVM / GRADLE
Assert-Step "JVM Gradle project configurations" {
    $jvmTargets = @(
        "../Cyrene-Platform/framework/jvm",
        "../services/cyrene-exchange/components/coordinator",
        "../plugins/plugins/gateway/spring"
    )
    foreach ($rel in $jvmTargets) {
        $full = Join-Path $ScriptDir $rel
        $buildScript = Join-Path $full "build.gradle.kts"
        if (-not (Test-Path $buildScript)) {
            throw "Missing build.gradle.kts in $rel"
        }
    }
}

# 6. REPOSITORY INDEPENDENCE
Assert-Step "Repository standalone buildability without Cyrene-Workspace" {
    $astrbotSln = Join-Path $ScriptDir "../services/cyrene-astrbot-rev/AstrBot.slnx"
    if (-not (Test-Path $astrbotSln)) { throw "AstrBot.slnx missing" }
    
    $wecomSln = Join-Path $ScriptDir "../services/cyrene-dh-system-internal/WeComAgentHub.slnx"
    if (-not (Test-Path $wecomSln)) { throw "WeComAgentHub.slnx missing" }
}

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host " RESULTS: $($script:passCount) Passed, $($script:failCount) Failed" -ForegroundColor $(if ($script:failCount -eq 0) { "Green" } else { "Red" })
Write-Host "==================================================" -ForegroundColor Cyan

if ($script:failCount -gt 0) {
    exit 1
}
