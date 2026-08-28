<#
.SYNOPSIS
    JetBrains Local Exact-Worktree Attachment Helper for Cyrene.

.DESCRIPTION
    Attaches C:\cwt task worktrees to the JetBrains IntelliJ / Rider project model
    as dynamic external content roots and Git VCS roots without modifying sibling checkouts
    or committing machine-specific paths. Supports multiple concurrent parallel worktrees.

.EXAMPLE
    .\ide-attach.ps1 -Action attach -Path C:\cwt\my-task-role
    .\ide-attach.ps1 -Action detach -Path C:\cwt\my-task-role
    .\ide-attach.ps1 -Action status
    .\ide-attach.ps1 -Action clean
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("attach", "detach", "status", "clean")]
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

function Invoke-AttachWorktree([string]$TargetWorktreePath, [string]$AgentRole) {
    if ([string]::IsNullOrWhiteSpace($TargetWorktreePath)) {
        throw "Target worktree path (-Path) is required for attach."
    }
    $fullPath = [System.IO.Path]::GetFullPath($TargetWorktreePath)
    if (-not (Test-Path -LiteralPath $fullPath)) {
        throw "Target worktree path '$fullPath' does not exist."
    }

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
