#!/usr/bin/env bash
# Cyrene Workspace — shell entry point for the canonical PowerShell helper.
#
# The safety rules live in agent-worktree.ps1 so Windows and shell callers do not
# acquire divergent branch, snapshot, or cleanup semantics. PowerShell 7 is
# available on the supported Windows developer appliance and can also be used on
# Linux/macOS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="$SCRIPT_DIR/agent-worktree.ps1"

if [[ ! -f "$HELPER" ]]; then
  echo "agent-worktree.ps1 is missing next to this shell entry point." >&2
  exit 1
fi

POWERSHELL=""
if command -v pwsh >/dev/null 2>&1; then
  POWERSHELL="$(command -v pwsh)"
elif command -v powershell >/dev/null 2>&1; then
  POWERSHELL="$(command -v powershell)"
fi

if [[ -z "$POWERSHELL" ]]; then
  echo "PowerShell 7 (pwsh) is required for the canonical agent-worktree helper." >&2
  echo "Install pwsh or invoke agent-worktree.ps1 directly on a supported developer host." >&2
  exit 127
fi

exec "$POWERSHELL" -NoProfile -File "$HELPER" "$@"
