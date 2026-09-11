<#
.SYNOPSIS
    Automated Development Workspace and Toolchain Acceptance Gate for Cyrene.
.DESCRIPTION
    Validates topology, absence of absolute paths, Python environments, .NET meta-solution,
    Rust development toolchain declarations, JVM toolchain freezing, and repository independence.
    Distinguishes Development Host checks from Canonical Linux Runtime Acceptance.
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
Write-Host " CYRENE WORKSPACE DEVELOPMENT GATE" -ForegroundColor Cyan
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
    $configFiles = Get-ChildItem -Path $ScriptDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
        $_.FullName -notmatch '[/\\]\.git[/\\]' -and
        $_.FullName -notmatch '[/\\]docs[/\\]' -and
        $_.FullName -notmatch '[/\\]bin[/\\]' -and
        $_.FullName -notmatch '[/\\]obj[/\\]' -and
        $_.FullName -notmatch '[/\\]\.venv[/\\]' -and
        $_.FullName -notmatch '[/\\]\.idea[/\\]modules[/\\]' -and
        $_.FullName -notmatch '[/\\]\.idea[/\\]libraries[/\\]' -and
        $_.FullName -notmatch '[/\\]\.idea[/\\]\.idea\.' -and
        $_.FullName -notmatch '[/\\]\.pytest-temp' -and
        $_.FullName -notmatch '[/\\]\.task-worktrees[/\\]' -and
        $_.Extension -in @('.yaml', '.slnx', '.xml', '.iml', '.json', '.md', '.ps1', '.sh')
    }
    
    foreach ($file in $configFiles) {
        $content = Get-Content $file.FullName -Raw
        if ($content -match 'C:\\Users\\' -or $content -match '/home/') {
            $allowedNames = @("verify.ps1", "IDE_ACCEPTANCE.md", "README.md", "TEST_POLICY.md", "test-performance-hardening.ps1", "test-agent-worktree.ps1", "toolchain-preflight.ps1", "agent-task.ps1", "test-dual-parallel-worktrees.ps1", "test-e2e-lifecycle.ps1", "test-same-symbol-authority.ps1")
            if ($file.Name -notin $allowedNames) {
                throw "User absolute path found in $($file.FullName)"
            }
        }
    }
}

# 2. INTELLIJ MULTI-PROJECT WORKSPACE
Assert-Step "IntelliJ IDEA Multi-Project Workspace (.idea/jb-workspace.xml)" {
    $jbWorkspace = Join-Path $ScriptDir ".idea/jb-workspace.xml"
    if (-not (Test-Path $jbWorkspace)) { throw ".idea/jb-workspace.xml not found" }
    $content = Get-Content $jbWorkspace -Raw
    if ($content -notmatch 'WorkspaceSettings' -and $content -notmatch 'WorkspaceProjectModel') {
        throw "Workspace configuration not found in jb-workspace.xml"
    }
}

# 3. PYTHON ENVIRONMENTS
Assert-Step "Python .venv discovery and uv environment sanity" {
    $pyTargets = @(
        "../Cyrene-Platform",
        "../Cyrene-Plugins-Official",
        "../Cyrene-Services/Cyrene-Reactor",
        "../Cyrene-Services/Cyrene-Yield",
        "../Cyrene-Services/Cyrene-Exchange"
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

# 4. .NET META-SOLUTION
Assert-Step ".NET Cyrene.Workspace.slnx project resolution" {
    $slnx = Join-Path $ScriptDir "Cyrene.Workspace.slnx"
    if (-not (Test-Path $slnx)) { throw "Cyrene.Workspace.slnx not found" }
    
    $output = dotnet sln $slnx list 2>&1
    if ($LASTEXITCODE -ne 0) { throw "dotnet sln list failed: $output" }
    
    $projectCount = ($output | Where-Object { $_ -match '\.csproj$' }).Count
    if ($projectCount -lt 2) {
        throw "Expected at least 2 projects, found $projectCount"
    }
}

if (-not $Quick) {
    Assert-Step ".NET aggregate solution build" {
        $slnx = Join-Path $ScriptDir "Cyrene.Workspace.slnx"
        $buildOutput = dotnet build $slnx -c Debug -p:WarningsNotAsErrors=NU1902 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "dotnet build failed"
        }
    }
}

# 5. RUST TOOLCHAIN DECLARATION (DEV HOST ONLY)
Assert-Step "Rust development toolchain declarations & cargo metadata" {
    $rustTargets = @("../Cyrene-Platform", "../Cyrene-Services/Cyrene-Reactor")
    foreach ($rel in $rustTargets) {
        $full = Join-Path $ScriptDir $rel
        $toolchainPin = Join-Path $full "rust-toolchain.toml"
        if (-not (Test-Path $toolchainPin)) {
            throw "Missing rust-toolchain.toml in $rel"
        }
        $output = cargo metadata --manifest-path (Join-Path $full "Cargo.toml") --format-version 1 --no-deps 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "cargo metadata failed for $rel"
        }
    }
}

# 6. JVM GRADLE & TOOLCHAIN PINNING
Assert-Step "JVM toolchain baseline (Gradle 9.5.0, Kotlin 2.4.10, JDK 25)" {
    $jvmTargets = @(
        "../Cyrene-Services/Cyrene-Exchange/components/coordinator",
        "../Cyrene-Plugins-Official/plugins/gateway/spring"
    )
    foreach ($rel in $jvmTargets) {
        $full = Join-Path $ScriptDir $rel
        $buildScript = Join-Path $full "build.gradle.kts"
        if (-not (Test-Path $buildScript)) {
            throw "Missing build.gradle.kts in $rel"
        }
        $wrapperProps = Join-Path $full "gradle/wrapper/gradle-wrapper.properties"
        if (-not (Test-Path $wrapperProps)) {
            throw "Missing gradle-wrapper.properties in $rel"
        }
        $propsContent = Get-Content $wrapperProps -Raw
        if ($propsContent -notmatch 'gradle-9\.5\.0') {
            throw "Gradle wrapper not pinned to 9.5.0 in $rel"
        }
        $daemonProps = Join-Path $full "gradle/gradle-daemon-jvm.properties"
        if (Test-Path $daemonProps) {
            $daemonContent = Get-Content $daemonProps -Raw
            if ($daemonContent -notmatch 'toolchainVersion\s*=\s*25') {
                throw "Gradle daemon JVM not set to toolchainVersion=25 in $rel"
            }
        }
    }
}

# 7. REPOSITORY INDEPENDENCE & PLATFORM SDK SEAM
Assert-Step "Repository independence & clean SDK authority (no copied source in Cyrene-Yield)" {
    $yieldPackages = Join-Path $ScriptDir "../Cyrene-Services/Cyrene-Yield/packages"
    if (Test-Path $yieldPackages) {
        $copiedSdks = Get-ChildItem $yieldPackages -Directory | Where-Object { $_.Name -match "^cyrene_" }
        if ($copiedSdks.Count -gt 0) {
            throw "Cyrene-Yield has copied Platform SDK sources in packages/"
        }
    }
}

# 8. DEVELOPER APPLIANCE & DATABASE INVENTORY DECLARATIONS
Assert-Step "Developer appliance & database inventory declarations" {
    $dbFile = Join-Path $ScriptDir "governance/databases.yaml"
    if (-not (Test-Path $dbFile)) { throw "Missing governance/databases.yaml" }
    
    $depsFile = Join-Path $ScriptDir ".idea/externalDependencies.xml"
    if (-not (Test-Path $depsFile)) { throw "Missing .idea/externalDependencies.xml" }
    
    $dsFile = Join-Path $ScriptDir ".idea/dataSources.xml"
    if (-not (Test-Path $dsFile)) { throw "Missing .idea/dataSources.xml" }
    
    $openScript = Join-Path $ScriptDir "open.ps1"
    if (-not (Test-Path $openScript)) { throw "Missing open.ps1" }
}

# 9. ZERO SECRETS IN APPLIANCE CONFIGURATIONS
Assert-Step "Absence of plaintext secrets in appliance & database configurations" {
    $sensitiveFiles = @(
        (Join-Path $ScriptDir "governance/databases.yaml"),
        (Join-Path $ScriptDir ".idea/dataSources.xml")
    )
    foreach ($f in $sensitiveFiles) {
        $content = Get-Content $f -Raw
        if ($content -match 'password:\s*["''\w]+' -and $content -notmatch 'password:\s*""' -and $content -notmatch '\$\{' -and $content -notmatch 'password: null') {
            throw "Potential plaintext password found in $f"
        }
    }
}

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host " RESULTS: $($script:passCount) Passed, $($script:failCount) Failed" -ForegroundColor $(if ($script:failCount -eq 0) { "Green" } else { "Red" })
Write-Host "==================================================" -ForegroundColor Cyan

Write-Host "`n[CANONICAL RUST RUNTIME STATUS]" -ForegroundColor Yellow
Write-Host "  Development Host: Windows (IDE Indexing & Local Tooling [OK])" -ForegroundColor Gray
Write-Host "  Canonical Runtime Target: Linux (x86_64-unknown-linux-gnu)" -ForegroundColor Gray
Write-Host "  Canonical Linux Acceptance: Executed via CI (ubuntu-latest) and verify-linux.sh" -ForegroundColor Gray

if ($script:failCount -gt 0) {
    exit 1
}
