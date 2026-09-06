<#
.SYNOPSIS
    Canonical Developer Toolchain Preflight and Path Authority for Cyrene.

.DESCRIPTION
    Validates developer toolchain readiness before agent tasks begin editing.
    Enforces canonical executable paths, eliminates Windows Store Python shims,
    discovers dotnet/cargo/rustfmt/uv/rg/protoc, and configures environment authority
    (e.g., CYRENE_PYTHON) for the active task process.

.EXAMPLE
    .\toolchain-preflight.ps1
    .\toolchain-preflight.ps1 -RequireProtoc -RequireRust
#>

[CmdletBinding()]
param(
    [switch]$RequireRust,
    [switch]$RequireDotNet,
    [switch]$RequireProtoc,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))

function Test-IsWindowsStoreShim([string]$ExecutablePath) {
    if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
        return $false
    }
    $normalized = [System.IO.Path]::GetFullPath($ExecutablePath)
    if ($normalized -like "*\Microsoft\WindowsApps\python*.exe" -or
        $normalized -like "*\WindowsApps\python*.exe") {
        return $true
    }
    return $false
}

function Find-CanonicalPython {
    # 1. CYRENE_PYTHON environment variable
    if (-not [string]::IsNullOrWhiteSpace($env:CYRENE_PYTHON)) {
        $candidate = [System.IO.Path]::GetFullPath($env:CYRENE_PYTHON)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            if (-not (Test-IsWindowsStoreShim $candidate)) {
                return $candidate
            }
        }
    }

    # 2. Local repository or workspace virtual environment (.venv)
    $venvCandidates = @(
        (Join-Path (Get-Location) ".venv\Scripts\python.exe"),
        (Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"),
        (Join-Path $WorkspaceRoot "..\Cyrene-Platform\.venv\Scripts\python.exe"),
        (Join-Path $WorkspaceRoot "..\Cyrene-Plugins-Official\.venv\Scripts\python.exe")
    )
    foreach ($venvPy in $venvCandidates) {
        if (Test-Path -LiteralPath $venvPy -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($venvPy)
        }
    }

    # 3. Standard LocalApp / Program Files Python installations
    $localAppCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Python\bin\python3.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe")
    )
    foreach ($localPy in $localAppCandidates) {
        if (Test-Path -LiteralPath $localPy -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($localPy)
        }
    }

    # 4. Search uv python installations
    $uvExe = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $uvExe) {
        try {
            $uvPy = (uv python find 2>$null | Select-Object -First 1)
            if (-not [string]::IsNullOrWhiteSpace($uvPy) -and (Test-Path -LiteralPath $uvPy -PathType Leaf)) {
                if (-not (Test-IsWindowsStoreShim $uvPy)) {
                    return [System.IO.Path]::GetFullPath($uvPy)
                }
            }
        } catch {}
    }

    # 5. Check PATH commands, explicitly filtering out WindowsApps
    $allPythons = @(Get-Command -All python.exe, python3.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($pathPy in $allPythons) {
        if (-not (Test-IsWindowsStoreShim $pathPy) -and (Test-Path -LiteralPath $pathPy -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($pathPy)
        }
    }

    return $null
}

function Find-CanonicalRipgrep {
    $rgCmd = Get-Command rg -ErrorAction SilentlyContinue
    if ($null -ne $rgCmd) {
        return $rgCmd.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE ".cargo\bin\rg.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\ripgrep\rg.exe"),
        (Join-Path $env:ProgramFiles "ripgrep\rg.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    # Check .cache/pkg directory if available
    $cacheDir = Join-Path $env:USERPROFILE ".cache\pkg"
    if (Test-Path -LiteralPath $cacheDir) {
        $found = Get-ChildItem -LiteralPath $cacheDir -Filter "rg.exe" -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $found) {
            return $found.FullName
        }
    }

    return $null
}

function Find-CanonicalProtoc {
    $protocCmd = Get-Command protoc -ErrorAction SilentlyContinue
    if ($null -ne $protocCmd) {
        return $protocCmd.Source
    }

    # Check NuGet package directories
    $nugetPackages = Join-Path $env:USERPROFILE ".nuget\packages\grpc.tools"
    if (Test-Path -LiteralPath $nugetPackages) {
        $tools = Get-ChildItem -LiteralPath $nugetPackages -Filter "protoc.exe" -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*windows_x64*" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($null -ne $tools) {
            return $tools.FullName
        }
    }

    return $null
}

$results = [ordered]@{}
$hasFailures = $false

# 1. Git
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($null -ne $gitCmd) {
    $gitVer = (& git --version)
    $results["git"] = [PSCustomObject]@{ Status = "PASS"; Path = $gitCmd.Source; Version = $gitVer }
} else {
    $results["git"] = [PSCustomObject]@{ Status = "FAIL"; Path = $null; Version = "git executable not found in PATH." }
    $hasFailures = $true
}

# 2. Python & Store Shim Detection
$barePythonCmd = Get-Command python -ErrorAction SilentlyContinue
$storeShimDetected = $false
if ($null -ne $barePythonCmd -and (Test-IsWindowsStoreShim $barePythonCmd.Source)) {
    $storeShimDetected = $true
}

$canonicalPython = Find-CanonicalPython
if ($null -ne $canonicalPython) {
    try {
        $pyVersion = (& $canonicalPython -c "import sys; print(sys.version.split()[0])" 2>&1 | Out-String).Trim()
        $results["python"] = [PSCustomObject]@{
            Status = "PASS"
            Path = $canonicalPython
            Version = "Python $pyVersion"
            StoreShimDetected = $storeShimDetected
        }
        # Set canonical environment authority
        $env:CYRENE_PYTHON = $canonicalPython
        $pyDir = Split-Path -Parent $canonicalPython
        if ($env:PATH -notlike "*$pyDir*") {
            $env:PATH = "$pyDir;" + $env:PATH
        }
    } catch {
        $results["python"] = [PSCustomObject]@{ Status = "FAIL"; Path = $canonicalPython; Version = "Failed execution: $($_.Exception.Message)" }
        $hasFailures = $true
    }
} else {
    $results["python"] = [PSCustomObject]@{
        Status = "FAIL"
        Path = if ($null -ne $barePythonCmd) { $barePythonCmd.Source } else { $null }
        Version = if ($storeShimDetected) { "REJECTED: Windows Store Python shim detected; install real Python." } else { "No canonical Python interpreter found." }
    }
    $hasFailures = $true
}

# 3. uv (Python Package Manager)
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCmd) {
    $uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $uvPath) {
        $uvCmd = [PSCustomObject]@{ Source = $uvPath }
    }
}
if ($null -ne $uvCmd) {
    $uvVer = (& $uvCmd.Source --version 2>&1 | Out-String).Trim()
    $results["uv"] = [PSCustomObject]@{ Status = "PASS"; Path = $uvCmd.Source; Version = $uvVer }
} else {
    $results["uv"] = [PSCustomObject]@{ Status = "WARN"; Path = $null; Version = "uv not found (recommended for python environment management)." }
}

# 4. .NET SDK
$dotnetCmd = Get-Command dotnet -ErrorAction SilentlyContinue
if ($null -ne $dotnetCmd) {
    $dotnetVer = (& dotnet --version 2>&1 | Out-String).Trim()
    $results["dotnet"] = [PSCustomObject]@{ Status = "PASS"; Path = $dotnetCmd.Source; Version = ".NET $dotnetVer" }
} else {
    $status = if ($RequireDotNet) { "FAIL" } else { "WARN" }
    if ($RequireDotNet) { $hasFailures = $true }
    $results["dotnet"] = [PSCustomObject]@{ Status = $status; Path = $null; Version = ".NET SDK not found." }
}

# 5. Rust / Cargo / Rustfmt
$cargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
if ($null -eq $cargoCmd) {
    $cargoPath = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
    if (Test-Path -LiteralPath $cargoPath) {
        $cargoCmd = [PSCustomObject]@{ Source = $cargoPath }
    }
}
$rustfmtCmd = Get-Command rustfmt -ErrorAction SilentlyContinue
if ($null -eq $rustfmtCmd) {
    $rustfmtPath = Join-Path $env:USERPROFILE ".cargo\bin\rustfmt.exe"
    if (Test-Path -LiteralPath $rustfmtPath) {
        $rustfmtCmd = [PSCustomObject]@{ Source = $rustfmtPath }
    }
}

if ($null -ne $cargoCmd) {
    $cargoVer = (& $cargoCmd.Source --version 2>&1 | Out-String).Trim()
    $results["cargo"] = [PSCustomObject]@{ Status = "PASS"; Path = $cargoCmd.Source; Version = $cargoVer }
} else {
    $status = if ($RequireRust) { "FAIL" } else { "WARN" }
    if ($RequireRust) { $hasFailures = $true }
    $results["cargo"] = [PSCustomObject]@{ Status = $status; Path = $null; Version = "Cargo not found." }
}

if ($null -ne $rustfmtCmd) {
    $rustfmtVer = (& $rustfmtCmd.Source --version 2>&1 | Out-String).Trim()
    $results["rustfmt"] = [PSCustomObject]@{ Status = "PASS"; Path = $rustfmtCmd.Source; Version = $rustfmtVer }
} else {
    $status = if ($RequireRust) { "FAIL" } else { "WARN" }
    if ($RequireRust) { $hasFailures = $true }
    $results["rustfmt"] = [PSCustomObject]@{ Status = $status; Path = $null; Version = "rustfmt not found." }
}

# 6. Ripgrep (rg)
$rgPath = Find-CanonicalRipgrep
if ($null -ne $rgPath) {
    $rgVer = (& $rgPath --version | Select-Object -First 1 | Out-String).Trim()
    $results["rg"] = [PSCustomObject]@{ Status = "PASS"; Path = $rgPath; Version = $rgVer }
} else {
    $results["rg"] = [PSCustomObject]@{ Status = "WARN"; Path = $null; Version = "ripgrep (rg) not found." }
}

# 7. Protoc / Grpc.Tools
$protocPath = Find-CanonicalProtoc
if ($null -ne $protocPath) {
    $protocVer = (& $protocPath --version 2>&1 | Out-String).Trim()
    $results["protoc"] = [PSCustomObject]@{ Status = "PASS"; Path = $protocPath; Version = $protocVer }
} else {
    $status = if ($RequireProtoc) { "FAIL" } else { "WARN" }
    if ($RequireProtoc) { $hasFailures = $true }
    $results["protoc"] = [PSCustomObject]@{ Status = $status; Path = $null; Version = "protoc not found." }
}

if (-not $Quiet) {
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host " CYRENE CANONICAL TOOLCHAIN PREFLIGHT" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor Cyan
    foreach ($key in $results.Keys) {
        $item = $results[$key]
        $color = switch ($item.Status) {
            "PASS" { "Green" }
            "WARN" { "Yellow" }
            "FAIL" { "Red" }
        }
        Write-Host ("  [{0,-4}] {1,-10}: {2} ({3})" -f $item.Status, $key, $item.Version, $item.Path) -ForegroundColor $color
    }
}

if ($hasFailures) {
    throw "Toolchain preflight FAILED. Canonical requirements not met."
}

return $results
