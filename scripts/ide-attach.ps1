<#
.SYNOPSIS
    JetBrains Local Exact-Worktree Attachment & Per-Agent IDE Project Helper for Cyrene.

.DESCRIPTION
    Manages JetBrains IntelliJ / Rider project models for C:\cwt task worktrees.
    Supports:
    1. Per-Agent IDE Project/Session (Recommended): Generates an isolated .idea project
       directly inside C:\cwt\<role> for strict 100% single-authority symbol resolution without multi-module collisions.
    2. Dynamic Workspace Attachment: Attaches task worktrees as external content roots.

.EXAMPLE
    .\ide-attach.ps1 -Action init-project -Path C:\cwt\my-task-role -Role my-task-role
    .\ide-attach.ps1 -Action attach -Path C:\cwt\my-task-role -Role my-task-role
    .\ide-attach.ps1 -Action detach -Path C:\cwt\my-task-role -Role my-task-role
    .\ide-attach.ps1 -Action status
    .\ide-attach.ps1 -Action clean
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("init-project", "attach", "detach", "status", "clean")]
    [string]$Action,

    [Parameter(Position = 1)]
    [string]$Path,

    [Parameter()]
    [string]$Role
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))
$IdeaDir = Join-Path $WorkspaceRoot ".idea"
$ModulesDir = Join-Path $IdeaDir "modules"
$ModulesXmlPath = Join-Path $IdeaDir "modules.xml"
$VcsXmlPath = Join-Path $IdeaDir "vcs.xml"

function Normalize-UrlPath([string]$FileSystemPath) {
    $full = [System.IO.Path]::GetFullPath($FileSystemPath)
    return ("file://" + $full.Replace("\", "/"))
}

function Ensure-ModulesDirectory {
    if (-not (Test-Path -LiteralPath $ModulesDir)) {
        New-Item -ItemType Directory -Path $ModulesDir -Force | Out-Null
    }
}

function Get-ModuleFileName([string]$TargetWorktreePath, [string]$AgentRole) {
    $roleName = if (-not [string]::IsNullOrWhiteSpace($AgentRole)) {
        $AgentRole
    } else {
        Split-Path ([System.IO.Path]::GetFullPath($TargetWorktreePath)) -Leaf
    }
    return "task-worktree-$roleName.iml"
}

function Invoke-InitPerAgentProject([string]$TargetWorktreePath, [string]$AgentRole) {
    if ([string]::IsNullOrWhiteSpace($TargetWorktreePath)) {
        throw "Target worktree path (-Path) is required for init-project."
    }
    $fullPath = [System.IO.Path]::GetFullPath($TargetWorktreePath)
    if (-not (Test-Path -LiteralPath $fullPath)) {
        throw "Target worktree path '$fullPath' does not exist."
    }

    $agentIdeaDir = Join-Path $fullPath ".idea"
    $agentModulesDir = Join-Path $agentIdeaDir "modules"
    New-Item -ItemType Directory -Path $agentIdeaDir, $agentModulesDir -Force | Out-Null

    $roleName = if (-not [string]::IsNullOrWhiteSpace($AgentRole)) { $AgentRole } else { Split-Path $fullPath -Leaf }
    $urlPath = Normalize-UrlPath $fullPath

    # 1. Module file
    $imlContent = @"
<?xml version="1.0" encoding="UTF-8"?>
<module type="GENERAL_MODULE" version="4">
  <component name="NewModuleRootManager" inherit-compiler-output="true">
    <exclude-output />
    <content url="$urlPath">
      <sourceFolder url="$urlPath/src" isTestSource="false" />
      <sourceFolder url="$urlPath/tests" isTestSource="true" />
      <excludeFolder url="$urlPath/target" />
      <excludeFolder url="$urlPath/.venv" />
      <excludeFolder url="$urlPath/bin" />
      <excludeFolder url="$urlPath/obj" />
    </content>
    <orderEntry type="inheritedJdk" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>
"@
    $imlPath = Join-Path $agentIdeaDir "$roleName.iml"
    [System.IO.File]::WriteAllText($imlPath, $imlContent, [System.Text.Encoding]::UTF8)

    # 2. modules.xml
    $modulesXmlContent = @"
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://`$PROJECT_DIR`$/.idea/$roleName.iml" filepath="`$PROJECT_DIR`$/.idea/$roleName.iml" />
    </modules>
  </component>
</project>
"@
    [System.IO.File]::WriteAllText((Join-Path $agentIdeaDir "modules.xml"), $modulesXmlContent, [System.Text.Encoding]::UTF8)

    # 3. vcs.xml
    $vcsXmlContent = @"
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="VcsDirectoryMappings">
    <mapping directory="$($fullPath.Replace('\', '/'))" vcs="Git" />
  </component>
</project>
"@
    [System.IO.File]::WriteAllText((Join-Path $agentIdeaDir "vcs.xml"), $vcsXmlContent, [System.Text.Encoding]::UTF8)

    # 4. misc.xml
    $miscXmlContent = @"
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2" />
</project>
"@
    [System.IO.File]::WriteAllText((Join-Path $agentIdeaDir "misc.xml"), $miscXmlContent, [System.Text.Encoding]::UTF8)

    Write-Output "INITIALIZED_PER_AGENT_IDE_PROJECT: Created isolated JetBrains project model in '$agentIdeaDir' (100% authority, zero sibling collision)."
}

function Invoke-AttachWorktree([string]$TargetWorktreePath, [string]$AgentRole) {
    if ([string]::IsNullOrWhiteSpace($TargetWorktreePath)) {
        throw "Target worktree path (-Path) is required for attach."
    }
    $fullPath = [System.IO.Path]::GetFullPath($TargetWorktreePath)
    if (-not (Test-Path -LiteralPath $fullPath)) {
        throw "Target worktree path '$fullPath' does not exist."
    }

    # Initialize per-agent project model inside the worktree first
    Invoke-InitPerAgentProject -TargetWorktreePath $fullPath -AgentRole $AgentRole

    Ensure-ModulesDirectory

    $modFileName = Get-ModuleFileName $fullPath $AgentRole
    $attachmentImlPath = Join-Path $ModulesDir $modFileName
    $urlPath = Normalize-UrlPath $fullPath

    $imlContent = @"
<?xml version="1.0" encoding="UTF-8"?>
<module type="GENERAL_MODULE" version="4">
  <component name="NewModuleRootManager" inherit-compiler-output="true">
    <exclude-output />
    <content url="$urlPath">
      <sourceFolder url="$urlPath/src" isTestSource="false" />
      <sourceFolder url="$urlPath/tests" isTestSource="true" />
      <excludeFolder url="$urlPath/target" />
      <excludeFolder url="$urlPath/.venv" />
      <excludeFolder url="$urlPath/bin" />
      <excludeFolder url="$urlPath/obj" />
    </content>
    <orderEntry type="inheritedJdk" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>
"@
    [System.IO.File]::WriteAllText($attachmentImlPath, $imlContent, [System.Text.Encoding]::UTF8)

    # Update modules.xml
    if (Test-Path -LiteralPath $ModulesXmlPath) {
        $xml = [xml](Get-Content -LiteralPath $ModulesXmlPath)
        $modulesNode = $xml.project.component.modules
        if ($null -ne $modulesNode) {
            $existing = $modulesNode.module | Where-Object { $_.fileurl -like "*$modFileName*" }
            if ($null -eq $existing -or $existing.Count -eq 0) {
                $newModule = $xml.CreateElement("module")
                $newModule.SetAttribute("fileurl", "file://`$PROJECT_DIR`$/.idea/modules/$modFileName")
                $newModule.SetAttribute("filepath", "`$PROJECT_DIR`$/.idea/modules/$modFileName")
                $modulesNode.AppendChild($newModule) | Out-Null
                $xml.Save($ModulesXmlPath)
            }
        }
    }

    # Update vcs.xml
    if (Test-Path -LiteralPath $VcsXmlPath) {
        $vcsXml = [xml](Get-Content -LiteralPath $VcsXmlPath)
        $vcsNode = $vcsXml.project.component | Where-Object { $_.name -eq "VcsDirectoryMappings" }
        if ($null -ne $vcsNode) {
            $existingMapping = $vcsNode.mapping | Where-Object { $_.directory -eq $fullPath.Replace("\", "/") -or $_.directory -eq $fullPath }
            if ($null -eq $existingMapping -or $existingMapping.Count -eq 0) {
                $newMapping = $vcsXml.CreateElement("mapping")
                $newMapping.SetAttribute("directory", $fullPath.Replace("\", "/"))
                $newMapping.SetAttribute("vcs", "Git")
                $vcsNode.AppendChild($newMapping) | Out-Null
                $vcsXml.Save($VcsXmlPath)
            }
        }
    }

    Write-Output "ATTACHED: JetBrains semantic index and VCS mapping bound to exact task worktree '$fullPath' (Module: $modFileName)."
}

function Invoke-DetachWorktree([string]$TargetWorktreePath, [string]$AgentRole) {
    $normalizedTarget = if (-not [string]::IsNullOrWhiteSpace($TargetWorktreePath)) {
        [System.IO.Path]::GetFullPath($TargetWorktreePath).Replace("\", "/")
    } else { $null }

    if (-not [string]::IsNullOrWhiteSpace($TargetWorktreePath) -or -not [string]::IsNullOrWhiteSpace($AgentRole)) {
        $candidateTarget = if (-not [string]::IsNullOrWhiteSpace($TargetWorktreePath)) { $TargetWorktreePath } else { "dummy" }
        $modFileName = Get-ModuleFileName $candidateTarget $AgentRole
        $attachmentImlPath = Join-Path $ModulesDir $modFileName
        if (Test-Path -LiteralPath $attachmentImlPath) {
            Remove-Item -LiteralPath $attachmentImlPath -Force -ErrorAction SilentlyContinue
        }

        # Remove from modules.xml
        if (Test-Path -LiteralPath $ModulesXmlPath) {
            $xml = [xml](Get-Content -LiteralPath $ModulesXmlPath)
            $modulesNode = $xml.project.component.modules
            if ($null -ne $modulesNode) {
                $toRemove = @($modulesNode.module | Where-Object { $_.fileurl -like "*$modFileName*" })
                foreach ($mod in $toRemove) {
                    $modulesNode.RemoveChild($mod) | Out-Null
                }
                $xml.Save($ModulesXmlPath)
            }
        }

        # Remove from vcs.xml
        if (Test-Path -LiteralPath $VcsXmlPath) {
            $vcsXml = [xml](Get-Content -LiteralPath $VcsXmlPath)
            $vcsNode = $vcsXml.project.component | Where-Object { $_.name -eq "VcsDirectoryMappings" }
            if ($null -ne $vcsNode) {
                $toRemove = @($vcsNode.mapping | Where-Object {
                    $_.directory -eq $normalizedTarget -or $_.directory -eq $TargetWorktreePath
                })
                foreach ($map in $toRemove) {
                    $vcsNode.RemoveChild($map) | Out-Null
                }
                $vcsXml.Save($VcsXmlPath)
            }
        }
        Write-Output "DETACHED: Removed JetBrains task worktree attachment metadata for '$TargetWorktreePath'."
    } else {
        # Full clean of all task-worktree attachments
        if (Test-Path -LiteralPath $ModulesDir) {
            Get-ChildItem -LiteralPath $ModulesDir -Filter "task-worktree*.iml" -File -ErrorAction SilentlyContinue |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
        }

        if (Test-Path -LiteralPath $ModulesXmlPath) {
            $xml = [xml](Get-Content -LiteralPath $ModulesXmlPath)
            $modulesNode = $xml.project.component.modules
            if ($null -ne $modulesNode) {
                $toRemove = @($modulesNode.module | Where-Object { $_.fileurl -like "*task-worktree*.iml*" })
                foreach ($mod in $toRemove) {
                    $modulesNode.RemoveChild($mod) | Out-Null
                }
                $xml.Save($ModulesXmlPath)
            }
        }

        if (Test-Path -LiteralPath $VcsXmlPath) {
            $vcsXml = [xml](Get-Content -LiteralPath $VcsXmlPath)
            $vcsNode = $vcsXml.project.component | Where-Object { $_.name -eq "VcsDirectoryMappings" }
            if ($null -ne $vcsNode) {
                $toRemove = @($vcsNode.mapping | Where-Object {
                    $_.directory -like "*cwt*" -or $_.directory -like "*/cwt/*" -or $_.directory -like "*AppData*Temp*"
                })
                foreach ($map in $toRemove) {
                    $vcsNode.RemoveChild($map) | Out-Null
                }
                $vcsXml.Save($VcsXmlPath)
            }
        }
        Write-Output "CLEANED: Removed all local JetBrains task worktree attachments."
    }
}

function Get-AttachmentStatus {
    $attachedModules = @()
    if (Test-Path -LiteralPath $ModulesDir) {
        $attachedModules = @(Get-ChildItem -LiteralPath $ModulesDir -Filter "task-worktree*.iml" -File -ErrorAction SilentlyContinue)
    }

    $attached = ($attachedModules.Count -gt 0)
    Write-Output "JETBRAINS TASK WORKTREE ATTACHMENT STATUS"
    Write-Output "----------------------------------------"
    Write-Output "Attached Count : $($attachedModules.Count)"
    
    $results = @()
    foreach ($mod in $attachedModules) {
        $targetRoot = $null
        try {
            $imlXml = [xml](Get-Content -LiteralPath $mod.FullName)
            $contentUrl = $imlXml.module.component.content.url
            $targetRoot = $contentUrl.Replace("file://", "")
        } catch {}
        Write-Output "  - Module: $($mod.Name) -> Root: $targetRoot"
        $results += [PSCustomObject]@{
            Attached = $true
            Target = $targetRoot
            ModuleFile = $mod.FullName
        }
    }

    if (-not $attached) {
        Write-Output "  (No task worktrees currently attached)"
        return [PSCustomObject]@{
            Attached = $false
            Target = $null
            ModuleFile = $null
        }
    }

    return $results
}

switch ($Action) {
    "init-project" {
        $target = $Path
        if ([string]::IsNullOrWhiteSpace($target) -and -not [string]::IsNullOrWhiteSpace($Role)) {
            $target = "C:\cwt\$Role"
        }
        Invoke-InitPerAgentProject -TargetWorktreePath $target -AgentRole $Role
    }
    "attach" {
        $target = $Path
        if ([string]::IsNullOrWhiteSpace($target) -and -not [string]::IsNullOrWhiteSpace($Role)) {
            $target = "C:\cwt\$Role"
        }
        Invoke-AttachWorktree -TargetWorktreePath $target -AgentRole $Role
    }
    "detach" {
        Invoke-DetachWorktree -TargetWorktreePath $Path -AgentRole $Role
    }
    "clean" {
        Invoke-DetachWorktree -TargetWorktreePath $null -AgentRole $null
    }
    "status" {
        Get-AttachmentStatus
    }
}
