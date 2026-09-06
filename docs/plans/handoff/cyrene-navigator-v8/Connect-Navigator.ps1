# ┌─────────────────────────────────────────────────────────────────────┐
# │  📄 Connect-Navigator.ps1                                             │
# │  Module: handoff.cyrene-navigator-v8.connect                          │
# │  Role: Launch the verified installed Navigator with temporary config.  │
# │                                                                      │
# │  模块职责：使用临时配置启动已校验的 Navigator 安装包                    │
# └─────────────────────────────────────────────────────────────────────┘

[CmdletBinding()]
param(
    [string] $ConfigPath,
    [switch] $ValidateOnly
)

$ErrorActionPreference = 'Stop'

Set-StrictMode -Version 2.0

$ExpectedNavigatorSha256 = '0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1'

function Assert-NonSensitiveText {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Value,
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Connection setting '$Name' must be a non-empty string."
    }
    if ($Value -ne $Value.Trim()) {
        throw "Connection setting '$Name' must not have leading or trailing whitespace."
    }
    if ($Value.IndexOfAny([char[]]@([char]0, [char]9, [char]10, [char]13)) -ge 0) {
        throw "Connection setting '$Name' contains a control character."
    }
}

function Assert-ServiceUrl {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Value,
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    Assert-NonSensitiveText -Value $Value -Name $Name
    try {
        $uri = [Uri]::new($Value, [UriKind]::Absolute)
    } catch {
        throw "Connection setting '$Name' must be an absolute service URL."
    }

    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "Connection setting '$Name' must not contain userinfo, a query, or a fragment."
    }
    if ($uri.Scheme -eq 'https') {
        return
    }
    if ($uri.Scheme -eq 'http' -and $uri.Host -eq '127.0.0.1' -and
        $uri.Authority -match '^127\.0\.0\.1(?::[0-9]+)?$') {
        return
    }
    throw "Connection setting '$Name' must use HTTPS, or HTTP with the literal host 127.0.0.1."
}

function Read-ConnectionConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Connection config is missing. Copy connection.example.json to connection.json first.'
    }
    try {
        $document = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw 'Connection config is not valid JSON.'
    }
    if ($null -eq $document -or $document -is [Array]) {
        throw 'Connection config must contain one JSON object.'
    }

    $allowedNames = @(
        'exchangeUrl',
        'persistenceUrl',
        'workspaceId',
        'accountProfileId',
        'deviceId',
        'model'
    )
    $requiredNames = @('exchangeUrl', 'persistenceUrl', 'workspaceId', 'accountProfileId', 'model')
    $propertyNames = @($document.PSObject.Properties.Name)
    $unknownNames = @($propertyNames | Where-Object { $_ -notin $allowedNames })
    if ($unknownNames.Count -gt 0) {
        throw 'Connection config may contain only non-sensitive connection fields; token fields are not accepted.'
    }

    $values = [ordered]@{}
    foreach ($name in $requiredNames) {
        $property = $document.PSObject.Properties[$name]
        if ($null -eq $property -or $property.Value -isnot [string]) {
            throw "Connection setting '$name' must be a non-empty JSON string."
        }
        Assert-NonSensitiveText -Value $property.Value -Name $name
        $values[$name] = $property.Value
    }
    $device = $document.PSObject.Properties['deviceId']
    if ($null -eq $device) {
        # Keep the desktop shell's documented default when no distinct device id is supplied.
        # 未提供设备标识时沿用桌面壳已有的 navigator 默认值。
        $values['deviceId'] = 'navigator'
    } elseif ($device.Value -isnot [string]) {
        throw "Connection setting 'deviceId' must be a non-empty JSON string when present."
    } else {
        Assert-NonSensitiveText -Value $device.Value -Name 'deviceId'
        $values['deviceId'] = $device.Value
    }
    return [pscustomobject] $values
}

function Resolve-VerifiedNavigator {
    $localAppData = [Environment]::GetEnvironmentVariable(
        'LOCALAPPDATA', [EnvironmentVariableTarget]::Process
    )
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        throw 'LOCALAPPDATA is unavailable; the installed Navigator location cannot be resolved.'
    }
    $candidate = Join-Path $localAppData 'Cyrene Navigator/cyrene-navigator-desktop.exe'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw 'The verified V8 Navigator executable was not found in the default installed location.'
    }
    try {
        $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).ProviderPath
        $actualHash = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    } catch {
        throw 'The installed Navigator executable could not be inspected.'
    }
    if ($actualHash -ne $ExpectedNavigatorSha256) {
        throw 'The default installed Navigator executable is not the verified V8 package.'
    }
    return [pscustomobject]@{ Path = $resolved; Sha256 = $actualHash }
}

function Set-ProcessSecretEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [Security.SecureString] $SecureValue
    )

    $bstr = [IntPtr]::Zero
    $plainText = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
        $plainText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        [Environment]::SetEnvironmentVariable($Name, $plainText, [EnvironmentVariableTarget]::Process)
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        $plainText = $null
    }
}

function Assert-SecureValuePresent {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString] $SecureValue,
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    if ($SecureValue.Length -eq 0) {
        throw "$Name token cannot be empty."
    }
}

function Restore-ProcessEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable] $Snapshot,
        [Parameter(Mandatory = $true)]
        [string[]] $Names
    )

    foreach ($name in @('CYRENE_EXCHANGE_TOKEN', 'CYRENE_SESSION_TOKEN')) {
        [Environment]::SetEnvironmentVariable($name, $null, [EnvironmentVariableTarget]::Process)
    }
    foreach ($name in $Names) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $Snapshot[$name],
            [EnvironmentVariableTarget]::Process
        )
    }
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $PSScriptRoot 'connection.json'
}

$connectionEnvironmentNames = @(
    'CYRENE_EXCHANGE_URL',
    'CYRENE_EXCHANGE_TOKEN',
    'CYRENE_HARNESS_MODEL',
    'CYRENE_PERSISTENCE_URL',
    'CYRENE_SESSION_TOKEN',
    'CYRENE_WORKSPACE_ID',
    'CYRENE_ACCOUNT_PROFILE_ID',
    'CYRENE_DEVICE_ID'
)
$environmentSnapshot = @{}
$environmentSnapshotCaptured = $false
$exchangeToken = $null
$sessionToken = $null
$result = $null

try {
    $navigator = Resolve-VerifiedNavigator
    $config = Read-ConnectionConfig -Path $ConfigPath
    Assert-ServiceUrl -Value $config.exchangeUrl -Name 'exchangeUrl'
    Assert-ServiceUrl -Value $config.persistenceUrl -Name 'persistenceUrl'

    if ($ValidateOnly) {
        $result = [ordered]@{
            status = 'validated'
            executable = $navigator.Path
            executableSha256 = $navigator.Sha256
            tokensRequested = $false
            processStarted = $false
        } | ConvertTo-Json -Compress
    } else {
        foreach ($name in $connectionEnvironmentNames) {
            $environmentSnapshot[$name] = [Environment]::GetEnvironmentVariable(
                $name, [EnvironmentVariableTarget]::Process
            )
        }
        $environmentSnapshotCaptured = $true

        $exchangeToken = Read-Host -Prompt 'Exchange token' -AsSecureString
        $sessionToken = Read-Host -Prompt 'Cyrene persistence token' -AsSecureString
        Assert-SecureValuePresent -SecureValue $exchangeToken -Name 'Exchange'
        Assert-SecureValuePresent -SecureValue $sessionToken -Name 'Cyrene persistence'

        [Environment]::SetEnvironmentVariable(
            'CYRENE_EXCHANGE_URL', $config.exchangeUrl, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            'CYRENE_HARNESS_MODEL', $config.model, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            'CYRENE_PERSISTENCE_URL', $config.persistenceUrl, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            'CYRENE_WORKSPACE_ID', $config.workspaceId, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            'CYRENE_ACCOUNT_PROFILE_ID', $config.accountProfileId, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            'CYRENE_DEVICE_ID', $config.deviceId, [EnvironmentVariableTarget]::Process
        )
        Set-ProcessSecretEnvironment -Name 'CYRENE_EXCHANGE_TOKEN' -SecureValue $exchangeToken
        Set-ProcessSecretEnvironment -Name 'CYRENE_SESSION_TOKEN' -SecureValue $sessionToken

        # No arguments carry credentials; the verified desktop inherits only this temporary Process scope.
        # 凭据不进入命令行，已校验的桌面进程只继承本次临时 Process 环境。
        $process = Start-Process -FilePath $navigator.Path -PassThru -WindowStyle Normal -ErrorAction Stop
        $result = [ordered]@{
            status = 'started'
            executable = $navigator.Path
            executableSha256 = $navigator.Sha256
            processId = $process.Id
            tokensRequested = $true
            processStarted = $true
        } | ConvertTo-Json -Compress
    }
} finally {
    if ($null -ne $exchangeToken) {
        $exchangeToken.Dispose()
        $exchangeToken = $null
    }
    if ($null -ne $sessionToken) {
        $sessionToken.Dispose()
        $sessionToken = $null
    }
    if ($environmentSnapshotCaptured) {
        Restore-ProcessEnvironment -Snapshot $environmentSnapshot -Names $connectionEnvironmentNames
    }
}

if ($null -ne $result) {
    Write-Output $result
}
