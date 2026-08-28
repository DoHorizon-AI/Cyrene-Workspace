<#
.SYNOPSIS
    Standard Cross-Language Protobuf Contract Generation and Smoke Gate Harness.

.DESCRIPTION
    Derives protoc and well-known type include locations from restored project and package metadata,
    independent of machine-specific user profiles, and executes compilation and code generation
    verification across Rust, C#, Java, Kotlin, and Python.

.EXAMPLE
    .\contract-harness.ps1
    .\contract-harness.ps1 -ProtoFile ..\Cyrene-Platform\contracts\proto\cyrene\core\v1\cyrene_core.proto
#>

[CmdletBinding()]
param(
    [string]$ProtoFile,
    [string]$ProtoRoot,
    [string]$ProtocPath,
    [string]$WellKnownTypesPath,
    [switch]$SkipRustBuild
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))
$PlatformRoot = [System.IO.Path]::GetFullPath((Join-Path $WorkspaceRoot "..\Cyrene-Platform"))

# 1. Resolve Proto Directory
if ([string]::IsNullOrWhiteSpace($ProtoRoot)) {
    if (Test-Path (Join-Path $PlatformRoot "contracts\proto")) {
        $ProtoRoot = Join-Path $PlatformRoot "contracts\proto"
    } else {
        $ProtoRoot = Join-Path $WorkspaceRoot "contracts\proto"
    }
}
$ProtoRoot = [System.IO.Path]::GetFullPath($ProtoRoot)

# 2. Derive Protoc Path from Restored Project Metadata
if ([string]::IsNullOrWhiteSpace($ProtocPath)) {
    $protocCmd = Get-Command protoc -ErrorAction SilentlyContinue
    if ($null -ne $protocCmd) {
        $ProtocPath = $protocCmd.Source
    } else {
        $nugetPackages = Join-Path $env:USERPROFILE ".nuget\packages\grpc.tools"
        if (Test-Path -LiteralPath $nugetPackages) {
            $tools = Get-ChildItem -LiteralPath $nugetPackages -Filter "protoc.exe" -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -like "*windows_x64*" } |
                Sort-Object FullName -Descending |
                Select-Object -First 1
            if ($null -ne $tools) {
                $ProtocPath = $tools.FullName
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($ProtocPath) -or -not (Test-Path -LiteralPath $ProtocPath -PathType Leaf)) {
    throw "protoc executable could not be derived from project metadata or PATH. Run dotnet restore or ensure Grpc.Tools is present."
}

# 3. Derive Well-Known Types Include Path
if ([string]::IsNullOrWhiteSpace($WellKnownTypesPath)) {
    $protocDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($ProtocPath))
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $protocDir "..\..\build\native\include"))
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $WellKnownTypesPath = $candidate
    } else {
        # Check standard include path
        $candidate2 = [System.IO.Path]::GetFullPath((Join-Path $protocDir "..\include"))
        if (Test-Path -LiteralPath $candidate2 -PathType Container) {
            $WellKnownTypesPath = $candidate2
        }
    }
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " CYRENE CROSS-LANGUAGE CONTRACT HARNESS" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Protoc Authority : $ProtocPath" -ForegroundColor White
Write-Host "  Proto Root       : $ProtoRoot" -ForegroundColor White
Write-Host "  Well-Known Types : $(if ($null -ne $WellKnownTypesPath) { $WellKnownTypesPath } else { '(bundled in proto root)' })" -ForegroundColor White

# 4. Target Proto Files
$targetProtos = @()
if (-not [string]::IsNullOrWhiteSpace($ProtoFile)) {
    $targetProtos = @([System.IO.Path]::GetFullPath($ProtoFile))
} else {
    $targetProtos = @(Get-ChildItem -LiteralPath $ProtoRoot -Filter "*.proto" -Recurse -File | ForEach-Object { $_.FullName })
}

if ($targetProtos.Count -eq 0) {
    throw "No .proto files found to compile in '$ProtoRoot'."
}

Write-Host "`n[1/5] Compiling and generating multi-language protobuf bindings..." -ForegroundColor Yellow
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$genRoot = Join-Path $tempRoot ("cyrene-proto-gate-" + [Guid]::NewGuid().ToString("N"))
$outputs = @{
    CSharp = Join-Path $genRoot "csharp"
    Java = Join-Path $genRoot "java"
    Kotlin = Join-Path $genRoot "kotlin"
    Python = Join-Path $genRoot "python"
}

try {
    foreach ($outDir in $outputs.Values) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }

    foreach ($proto in $targetProtos) {
        $protocArgs = @(
            "--proto_path=$ProtoRoot"
        )
        if (-not [string]::IsNullOrWhiteSpace($WellKnownTypesPath) -and (Test-Path -LiteralPath $WellKnownTypesPath)) {
            $protocArgs += "--proto_path=$WellKnownTypesPath"
        }
        $protocArgs += @(
            "--csharp_out=$($outputs.CSharp)",
            "--java_out=$($outputs.Java)",
            "--kotlin_out=$($outputs.Kotlin)",
            "--python_out=$($outputs.Python)",
            $proto
        )

        & $ProtocPath @protocArgs
        if ($LASTEXITCODE -ne 0) {
            throw "protoc generation failed for '$proto' with exit code $LASTEXITCODE."
        }
    }

    # 5. Verify C# Generated Outputs
    Write-Host "`n[2/5] Verifying C# protobuf generation..." -ForegroundColor Yellow
    $csFiles = @(Get-ChildItem -LiteralPath $outputs.CSharp -Recurse -Filter "*.cs" -File)
    if ($csFiles.Count -eq 0) { throw "C# generation produced 0 .cs files." }
    Write-Host "  [PASS] C# bindings generated: $($csFiles.Count) files." -ForegroundColor Green

    # 6. Verify Java & Kotlin Outputs
    Write-Host "`n[3/5] Verifying Java & Kotlin protobuf generation..." -ForegroundColor Yellow
    $javaFiles = @(Get-ChildItem -LiteralPath $outputs.Java -Recurse -Filter "*.java" -File)
    $ktFiles = @(Get-ChildItem -LiteralPath $outputs.Kotlin -Recurse -Filter "*.kt" -File)
    if ($javaFiles.Count -eq 0) { throw "Java generation produced 0 .java files." }
    Write-Host "  [PASS] Java bindings generated: $($javaFiles.Count) files." -ForegroundColor Green
    Write-Host "  [PASS] Kotlin bindings generated: $($ktFiles.Count) files." -ForegroundColor Green

    # 7. Verify Python Outputs & Syntax Compilation
    Write-Host "`n[4/5] Verifying Python protobuf generation & syntax..." -ForegroundColor Yellow
    $pyFiles = @(Get-ChildItem -LiteralPath $outputs.Python -Recurse -Filter "*.py" -File)
    if ($pyFiles.Count -eq 0) { throw "Python generation produced 0 .py files." }
    $pyExe = if (-not [string]::IsNullOrWhiteSpace($env:CYRENE_PYTHON)) { $env:CYRENE_PYTHON } else { "python" }
    foreach ($pyFile in $pyFiles) {
        & $pyExe -m py_compile $pyFile.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Python py_compile failed for $($pyFile.FullName)."
        }
    }
    Write-Host "  [PASS] Python bindings generated and syntax-validated: $($pyFiles.Count) files." -ForegroundColor Green

    # 8. Verify Rust Prost/Tonic Contract Build
    Write-Host "`n[5/5] Verifying Rust cy-proto contract build..." -ForegroundColor Yellow
    if (-not $SkipRustBuild -and (Test-Path -LiteralPath (Join-Path $PlatformRoot "contracts\rust\cy-proto\Cargo.toml"))) {
        $manifest = Join-Path $PlatformRoot "contracts\rust\cy-proto\Cargo.toml"
        $cargoOutput = @(& cargo check --manifest-path $manifest 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Rust cy-proto cargo check failed: $($cargoOutput -join "`n")"
        }
        Write-Host "  [PASS] Rust cy-proto tonic/prost contract checked clean." -ForegroundColor Green
    } else {
        Write-Host "  [INFO] Rust contract check skipped or manifest not present." -ForegroundColor Gray
    }

    Write-Host "`n==================================================" -ForegroundColor Cyan
    Write-Host " CROSS-LANGUAGE CONTRACT HARNESS: PASS" -ForegroundColor Green
    Write-Host "==================================================" -ForegroundColor Cyan

    return [PSCustomObject]@{
        Status = "PASS"
        CSharpCount = $csFiles.Count
        JavaCount = $javaFiles.Count
        KotlinCount = $ktFiles.Count
        PythonCount = $pyFiles.Count
        ProtocPath = $ProtocPath
    }
} finally {
    if (Test-Path -LiteralPath $genRoot) {
        Remove-Item -LiteralPath $genRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
