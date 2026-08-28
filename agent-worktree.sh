#!/usr/bin/env bash
# ==============================================================================
# Cyrene Workspace — Parallel Agent Worktree Isolation Helper
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_PATH="${SCRIPT_DIR}/repositories.yaml"

ACTION="${1:-}"
shift || true

REPO=""
BRANCH=""
BASE=""
SHA=""
ROLE=""
PATH_OVERRIDE=""
ROOT_OVERRIDE=""
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -Repo|--repo|-Repository|--repository)
      REPO="$2"; shift 2 ;;
    -Branch|--branch)
      BRANCH="$2"; shift 2 ;;
    -Base|--base)
      BASE="$2"; shift 2 ;;
    -Sha|--sha)
      SHA="$2"; shift 2 ;;
    -Role|--role)
      ROLE="$2"; shift 2 ;;
    -Path|--path|-WorktreePath|--worktree-path)
      PATH_OVERRIDE="$2"; shift 2 ;;
    -Root|--root|-WorktreeRoot|--worktree-root)
      ROOT_OVERRIDE="$2"; shift 2 ;;
    -Force|--force)
      FORCE=1; shift 1 ;;
    *)
      if [[ -z "$REPO" ]]; then
        REPO="$1"; shift 1
      else
        echo "Unknown argument: $1" >&2
        exit 1
      fi
      ;;
  esac
done

get_worktree_root() {
  if [[ -n "$ROOT_OVERRIDE" ]]; then
    mkdir -p "$ROOT_OVERRIDE"
    (cd "$ROOT_OVERRIDE" && pwd)
    return
  fi
  if [[ -n "${CYRENE_WORKTREE_ROOT:-}" ]]; then
    mkdir -p "$CYRENE_WORKTREE_ROOT"
    (cd "$CYRENE_WORKTREE_ROOT" && pwd)
    return
  fi
  local fallback="/tmp/cwt"
  mkdir -p "$fallback"
  (cd "$fallback" && pwd)
}

resolve_repo_path() {
  local query="$1"
  local q_lower
  q_lower="$(echo "$query" | tr '[:upper:]' '[:lower:]')"

  # Direct alias mappings
  case "$q_lower" in
    platform|cyrene-platform) echo "${SCRIPT_DIR}/../Cyrene-Platform"; return ;;
    plugins|cyrene-plugins) echo "${SCRIPT_DIR}/../plugins"; return ;;
    astrbot|astrbot-rev|cyrene-astrbot-rev) echo "${SCRIPT_DIR}/../services/cyrene-astrbot-rev"; return ;;
    dh-system|internal|cyrene-dh-system-internal) echo "${SCRIPT_DIR}/../services/cyrene-dh-system-internal"; return ;;
    reactor|cyrene-reactor) echo "${SCRIPT_DIR}/../services/cyrene-reactor"; return ;;
    yield|cyrene-yield) echo "${SCRIPT_DIR}/../services/Cyrene-Yield"; return ;;
    exchange|cyrene-exchange) echo "${SCRIPT_DIR}/../services/cyrene-exchange"; return ;;
    catalyst|cyrene-catalyst) echo "${SCRIPT_DIR}/../services/cyrene-catalyst"; return ;;
    echo|cyrene-echo) echo "${SCRIPT_DIR}/../services/cyrene-echo"; return ;;
    navigator|cyrene-navigator) echo "${SCRIPT_DIR}/../services/cyrene-navigator"; return ;;
  esac

  if [[ -d "$query/.git" ]]; then
    (cd "$query" && pwd)
    return
  fi

  echo "Unknown repository: $query" >&2
  exit 1
}

get_registry_path() {
  local root="$1"
  echo "${root}/.registry.json"
}

action_create() {
  if [[ -z "$REPO" ]] || [[ -z "$BRANCH" ]] || [[ -z "$ROLE" ]]; then
    echo "Usage: $0 create --repo <repo> --branch <branch> --role <role> [--base <base>]" >&2
    exit 1
  fi

  local repo_dir
  repo_dir="$(resolve_repo_path "$REPO")"
  local wt_root
  wt_root="$(get_worktree_root)"
  local target_path="${PATH_OVERRIDE:-${wt_root}/${ROLE}}"

  echo "=== CREATE TASK WORKTREE ==="
  echo "  Role:        ${ROLE}"
  echo "  Repository:  ${REPO} (${repo_dir})"
  echo "  Branch:      ${BRANCH}"
  echo "  Destination: ${target_path}"

  if [[ -d "$target_path" ]]; then
    if [[ -f "$target_path/.git" ]]; then
      local cur_branch
      cur_branch="$(git -C "$target_path" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      if [[ "$cur_branch" == "$BRANCH" ]]; then
        echo "  [IDEMPOTENT] Worktree already exists for role '$ROLE' on branch '$BRANCH'."
        return 0
      fi
    fi
    echo "Error: Target path '$target_path' already exists. Refusing to overwrite." >&2
    exit 1
  fi

  # Safe fetch
  if git -C "$repo_dir" remote | grep -q "^origin$"; then
    git -C "$repo_dir" fetch origin >/dev/null 2>&1 || true
  fi

  local base_sha=""
  local base_ref="${BASE:-origin/develop}"
  base_sha="$(git -C "$repo_dir" rev-parse --verify "${base_ref}^{commit}" 2>/dev/null || true)"
  if [[ -z "$base_sha" ]]; then
    base_ref="develop"
    base_sha="$(git -C "$repo_dir" rev-parse --verify "${base_ref}^{commit}" 2>/dev/null || true)"
  fi
  if [[ -z "$base_sha" ]]; then
    base_ref="main"
    base_sha="$(git -C "$repo_dir" rev-parse --verify "${base_ref}^{commit}" 2>/dev/null || true)"
  fi
  if [[ -z "$base_sha" ]]; then
    base_ref="HEAD"
    base_sha="$(git -C "$repo_dir" rev-parse --verify "${base_ref}^{commit}" 2>/dev/null || true)"
  fi

  if git -C "$repo_dir" rev-parse --verify "refs/heads/$BRANCH" >/dev/null 2>&1; then
    git -C "$repo_dir" worktree add "$target_path" "$BRANCH"
  else
    git -C "$repo_dir" worktree add -b "$BRANCH" "$target_path" "$base_sha"
  fi

  echo "  [SUCCESS] Task worktree created at $target_path"
}

action_snapshot() {
  if [[ -z "$REPO" ]] || [[ -z "$SHA" ]] || [[ -z "$ROLE" ]]; then
    echo "Usage: $0 snapshot --repo <repo> --sha <sha> --role <role>" >&2
    exit 1
  fi

  local repo_dir
  repo_dir="$(resolve_repo_path "$REPO")"
  local wt_root
  wt_root="$(get_worktree_root)"
  local target_path="${PATH_OVERRIDE:-${wt_root}/${ROLE}}"

  echo "=== CREATE IMMUTABLE SNAPSHOT WORKTREE ==="
  echo "  Role:        ${ROLE}"
  echo "  Repository:  ${REPO} (${repo_dir})"
  echo "  Target SHA:  ${SHA}"
  echo "  Destination: ${target_path}"

  local resolved_sha
  resolved_sha="$(git -C "$repo_dir" rev-parse --verify "${SHA}^{commit}" 2>/dev/null || true)"
  if [[ -z "$resolved_sha" ]]; then
    git -C "$repo_dir" fetch origin "$SHA" >/dev/null 2>&1 || true
    resolved_sha="$(git -C "$repo_dir" rev-parse --verify "${SHA}^{commit}" 2>/dev/null || true)"
  fi

  if [[ -z "$resolved_sha" ]]; then
    echo "Error: Cannot resolve commit SHA '$SHA' in $REPO" >&2
    exit 1
  fi

  if [[ -d "$target_path" ]]; then
    echo "Error: Target path '$target_path' already exists." >&2
    exit 1
  fi

  git -C "$repo_dir" worktree add --detach "$target_path" "$resolved_sha"

  cat <<EOF > "${target_path}/.cyrene-snapshot.json"
{
  "type": "CYRENE_INTEGRATION_SNAPSHOT",
  "role": "${ROLE}",
  "repo": "${REPO}",
  "sha": "${resolved_sha}",
  "immutableSnapshot": true
}
EOF

  echo "  [SUCCESS] Immutable snapshot worktree created at $target_path"
}

action_status() {
  local wt_root
  wt_root="$(get_worktree_root)"
  echo "===================================================================================================="
  echo " CYRENE PARALLEL AGENT WORKTREE STATUS"
  echo " Worktree Root: ${wt_root}"
  echo "===================================================================================================="
  printf "%-18s %-16s %-10s %-32s %s\n" "ROLE" "REPO" "MODE" "REF/SHA" "PATH"
  printf "%-18s %-16s %-10s %-32s %s\n" "------------------" "----------------" "----------" "--------------------------------" "--------------------------------"

  local repos=("Cyrene-Platform" "plugins" "services/cyrene-astrbot-rev" "services/cyrene-dh-system-internal" "services/cyrene-reactor" "services/Cyrene-Yield" "services/cyrene-exchange")
  for r in "${repos[@]}"; do
    local r_dir="${SCRIPT_DIR}/../${r}"
    if [[ -d "$r_dir" ]]; then
      local wt_list
      wt_list="$(git -C "$r_dir" worktree list --porcelain 2>/dev/null || true)"
      local cur_path=""
      local cur_head=""
      local cur_branch=""
      local is_detached=0

      while IFS= read -r line; do
        if [[ "$line" =~ ^worktree[[:space:]]+(.+) ]]; then
          cur_path="${BASH_REMATCH[1]}"
          cur_head=""
          cur_branch=""
          is_detached=0
        elif [[ "$line" =~ ^HEAD[[:space:]]+([0-9a-fA-F]+) ]]; then
          cur_head="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^branch[[:space:]]+refs/heads/(.+) ]]; then
          cur_branch="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^detached ]]; then
          is_detached=1
        elif [[ -z "$line" && -n "$cur_path" ]]; then
          local norm_cur
          norm_cur="$(cd "$cur_path" 2>/dev/null && pwd || echo "$cur_path")"
          local norm_repo
          norm_repo="$(cd "$r_dir" 2>/dev/null && pwd || echo "$r_dir")"
          if [[ "$norm_cur" != "$norm_repo" ]]; then
            local role="(discovered)"
            local mode="writer"
            local ref="$cur_branch"
            if [[ $is_detached -eq 1 ]]; then
              mode="snapshot"
              ref="${cur_head:0:7}"
            fi
            local repo_name
            repo_name="$(basename "$r")"
            printf "%-18s %-16s %-10s %-32s %s\n" "$role" "$repo_name" "$mode" "$ref" "$cur_path"
          fi
          cur_path=""
        fi
      done <<< "$wt_list"
    fi
  done
  echo "===================================================================================================="
}

action_remove() {
  local wt_root
  wt_root="$(get_worktree_root)"
  local target_path="${PATH_OVERRIDE:-${wt_root}/${ROLE}}"

  if [[ ! -d "$target_path" ]]; then
    echo "Worktree path '$target_path' does not exist."
    return 0
  fi

  # Check dirty
  local dirty
  dirty="$(git -C "$target_path" status --porcelain 2>/dev/null || true)"
  if [[ -n "$dirty" && $FORCE -eq 0 ]]; then
    echo "Error: Worktree at '$target_path' is DIRTY. Removal REFUSED. Use --force to override." >&2
    exit 1
  fi

  # Remove
  git -C "$target_path" worktree remove "$target_path" ${FORCE:+--force} 2>/dev/null || rm -rf "$target_path"
  echo "  [SUCCESS] Worktree at '$target_path' safely removed."
}

case "$ACTION" in
  create)   action_create ;;
  snapshot) action_snapshot ;;
  status|list) action_status ;;
  remove|cleanup) action_remove ;;
  *)
    echo "Usage: $0 {create|snapshot|status|remove} [options]" >&2
    exit 1
    ;;
esac
