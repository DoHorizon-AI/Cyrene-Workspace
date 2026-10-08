#!/usr/bin/env python3
"""Plan and safely apply Git branch/worktree housekeeping.

The tool uses Git ancestry and live GitHub state as evidence. Age is a review
signal only; it never authorizes deletion. 任务清理只按可验证的合并血缘执行。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

VERSION = 1
DEFAULT_RECENT_DAYS = 14
MAX_ACTIONS = 300
REMOTE_REVALIDATION_BATCH = 10
ALWAYS_KEEP = {"main", "develop", "release", "master", "trunk", "default"}
CACHE_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "coverage",
    "node_modules",
    "target",
    "venv",
}


class HygieneError(RuntimeError):
    """Raised when the evidence needed for a safe plan is unavailable."""


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run Git without a shell and return UTF-8 output.

    Raises HygieneError on a failed command so callers can fail closed.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise HygieneError(f"git {shlex.join(args)} failed")
    return completed.stdout.strip()


def gh_json(*args: str) -> Any:
    """Call gh and decode JSON while omitting potentially sensitive stderr."""
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise HygieneError("GitHub CLI evidence is unavailable")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HygieneError("GitHub CLI returned invalid JSON") from exc


def remote_repo_slug(repo: Path, remote: str) -> str:
    """Return owner/name for a GitHub remote, refusing unknown hosts."""
    url = run_git(repo, "config", "--get", f"remote.{remote}.url")
    match = re.search(r"(?:github\.com[:/])([^/:]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise HygieneError("remote is not a supported github.com repository")
    return match.group(1)


def get_github_evidence(repo: Path, remote: str, target: str) -> dict[str, Any]:
    """Read repository defaults, open PR heads, and protected branch names."""
    slug = remote_repo_slug(repo, remote)
    metadata = gh_json("api", f"repos/{slug}")
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise HygieneError("GitHub default branch is unavailable")

    # Tabular API projection paginates safely without parsing gh's concatenated
    # JSON page stream.
    open_pr_rows = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--jq",
            '.[] | [.head.ref, .head.sha, .base.ref, (.head.repo.full_name // "")] | @tsv',
            f"repos/{slug}/pulls?state=open&per_page=100",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if open_pr_rows.returncode != 0:
        raise HygieneError("GitHub open pull request evidence is unavailable")
    open_prs: list[dict[str, str]] = []
    for line in open_pr_rows.stdout.splitlines():
        cells = line.split("\t")
        if len(cells) != 4 or not cells[0] or not cells[1]:
            raise HygieneError("GitHub returned incomplete open pull request evidence")
        open_prs.append(
            {"head": cells[0], "sha": cells[1], "base": cells[2], "repository": cells[3]}
        )

    protected_result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--jq",
            ".[].name",
            f"repos/{slug}/branches?protected=true&per_page=100",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if protected_result.returncode != 0:
        raise HygieneError("GitHub protected branch evidence is unavailable")
    protected = sorted(name for name in protected_result.stdout.splitlines() if name)

    # Positive merged-PR evidence is optional. It can rescue exact squash or
    # rebase heads, but missing/limited history never authorizes deletion.
    merged_result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "merged",
            "--base",
            target,
            "--json",
            "headRefName,headRefOid,baseRefName,mergeCommit,mergedAt",
            "--limit",
            "1000",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    merged_prs: list[dict[str, str]] = []
    if merged_result.returncode == 0:
        try:
            rows = json.loads(merged_result.stdout)
            for row in rows:
                merge_commit = row.get("mergeCommit") or {}
                if row.get("headRefName") and row.get("headRefOid") and merge_commit.get("oid"):
                    merged_prs.append(
                        {
                            "head": row["headRefName"],
                            "sha": row["headRefOid"],
                            "base": row.get("baseRefName", target),
                            "merge_sha": merge_commit["oid"],
                            "merged_at": row.get("mergedAt", ""),
                        }
                    )
        except (json.JSONDecodeError, AttributeError, TypeError):
            # No positive evidence is safer than accepting a malformed record.
            merged_prs = []

    return {
        "slug": slug,
        "default_branch": default_branch,
        "protected": protected,
        "open_prs": sorted(open_prs, key=lambda row: (row["head"], row["sha"], row["base"])),
        "merged_prs": merged_prs,
    }


def refresh_remote(repo: Path, remote: str) -> dict[str, str]:
    """Fetch/prune and return live remote heads from ls-remote."""
    run_git(repo, "fetch", "--prune", remote, f"+refs/heads/*:refs/remotes/{remote}/*")
    output = run_git(repo, "ls-remote", "--heads", remote)
    heads: dict[str, str] = {}
    for line in output.splitlines():
        try:
            sha, ref = line.split("\t", 1)
        except ValueError as exc:
            raise HygieneError("remote returned malformed branch evidence") from exc
        if not ref.startswith("refs/heads/"):
            raise HygieneError("remote returned an unexpected ref")
        heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def worktrees(repo: Path) -> list[dict[str, Any]]:
    """Parse Git's porcelain worktree list and retain lock state."""
    output = run_git(repo, "worktree", "list", "--porcelain")
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current:
                if "path" not in current:
                    raise HygieneError("Git returned incomplete worktree evidence")
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(Path(value).resolve())
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
        elif key == "locked":
            current["locked"] = True
    if not entries:
        raise HygieneError("Git returned no worktree evidence")
    return entries


def is_within(path: Path, root: Path) -> bool:
    """Check resolved containment without relying on string prefixes."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def process_cwds() -> tuple[list[str], bool]:
    """Return observed Linux process working directories and scan completeness."""
    proc = Path("/proc")
    if not proc.is_dir():
        return [], False
    paths: list[str] = []
    complete = True
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue  # Ignore this verifier; a tool must start outside removed worktrees.
        try:
            if entry.stat().st_uid != os.getuid():
                continue  # Other users cannot hold this user's private worktree open.
            paths.append(str((entry / "cwd").resolve(strict=True)))
        except FileNotFoundError:
            continue  # Process exited while the bounded scan was running.
        except PermissionError:
            try:
                command = (
                    (entry / "cmdline")
                    .read_bytes()[:256]
                    .split(b"\0", 1)[0]
                    .decode("utf-8", "replace")
                )
            except OSError:
                command = ""
            # These session supervisors are not application workers. Their
            # child shells/processes remain visible and are checked separately.
            if command.startswith(("/usr/lib/systemd/systemd", "sshd:")) or command == "(sd-pam)":
                continue
            complete = False
        except OSError:
            continue
    return paths, complete


def ignored_noncache(repo: Path) -> list[str]:
    """List ignored worktree data that cannot be presumed rebuildable."""
    output = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        check=False,
        capture_output=True,
    )
    if output.returncode != 0:
        raise HygieneError("ignored-file evidence is unavailable")
    paths = [
        Path(raw.decode("utf-8", "surrogateescape")) for raw in output.stdout.split(b"\0") if raw
    ]
    return [str(path) for path in paths if not (set(path.parts) & CACHE_PARTS)]


def status_dirty(repo: Path) -> bool:
    """Detect tracked changes and ordinary untracked files."""
    return bool(run_git(repo, "status", "--porcelain=v1", "--untracked-files=all"))


def commit_time(repo: Path, sha: str) -> int:
    """Read a commit timestamp, failing closed for missing/non-commit tips."""
    value = run_git(repo, "show", "-s", "--format=%ct", f"{sha}^{{commit}}")
    try:
        return int(value)
    except ValueError as exc:
        raise HygieneError("branch tip has no readable commit time") from exc


def is_ancestor(repo: Path, candidate: str, target: str) -> bool:
    """Test ancestry without treating PR state or age as a substitute."""
    completed = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", candidate, target],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode not in (0, 1):
        raise HygieneError("Git ancestry evidence is unavailable")
    return completed.returncode == 0


def is_keep_branch(name: str, keep: set[str], protected: set[str], default: str) -> bool:
    """Recognize conventional, explicitly retained, default, and protected refs."""
    return (
        name in ALWAYS_KEEP
        or name == default
        or name in keep
        or name in protected
        or name.startswith("release/")
    )


def pr_merge_proof(
    repo: Path, name: str, sha: str, target: str, merged_prs: list[dict[str, str]]
) -> str | None:
    """Return a merge commit proving an exact squashed/rebased PR head."""
    for pr in merged_prs:
        if (
            pr["head"] == name
            and pr["sha"] == sha
            and pr["base"] == target
            and is_ancestor(repo, pr["merge_sha"], target)
        ):
            return pr["merge_sha"]
    return None


def inspect_repo(
    repo_value: str,
    *,
    remote: str,
    target: str,
    recent_days: int,
    keep: Sequence[str],
    only_branches: Sequence[str],
    completed_worktrees: Sequence[str],
    plan_id: str,
) -> dict[str, Any]:
    """Create a reviewable per-repository inventory and safe-action list."""
    repo = Path(repo_value).expanduser().resolve()
    top = Path(run_git(repo, "rev-parse", "--show-toplevel")).resolve()
    common_dir = Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    wt = worktrees(top)
    primary = Path(wt[0]["path"])
    context_head = run_git(top, "rev-parse", "HEAD")
    live_heads = refresh_remote(top, remote)
    github = get_github_evidence(top, remote, target)
    target_sha = live_heads.get(target)
    if not target_sha:
        raise HygieneError(f"target {remote}/{target} is not a live remote head")
    target_tracking = run_git(top, "rev-parse", f"refs/remotes/{remote}/{target}")
    if target_tracking != target_sha:
        raise HygieneError("fetched target does not match the live remote target")
    age_cutoff = int(dt.datetime.now(dt.UTC).timestamp()) - recent_days * 86400
    explicit_completed = {str(Path(path).expanduser().resolve()) for path in completed_worktrees}
    only = set(only_branches)
    keep_set = set(keep)
    open_names = {pr["head"] for pr in github["open_prs"]}
    protected_set = set(github["protected"])
    proc_paths, proc_scan_complete = process_cwds()
    remote_snapshot = dict(sorted(live_heads.items()))

    # Each eligible action receives a hidden ref before any branch deletion.
    actions: list[dict[str, Any]] = []
    branch_rows: list[dict[str, Any]] = []
    wt_rows: list[dict[str, Any]] = []
    local_refs = run_git(
        top, "for-each-ref", "--format=%(refname:short)%09%(objectname)", "refs/heads"
    )
    local_heads: dict[str, str] = {}
    for row in local_refs.splitlines():
        name, sep, sha = row.partition("\t")
        if sep:
            local_heads[name] = sha
    checked_out = {item.get("branch"): item for item in wt if item.get("branch")}
    open_names = {pr["head"] for pr in github["open_prs"]}

    def branch_analysis(name: str, sha: str, scope: str) -> dict[str, Any]:
        timestamp = commit_time(top, sha)
        stale = timestamp <= age_cutoff
        ancestry = is_ancestor(top, sha, target_sha)
        proof = None if ancestry else pr_merge_proof(top, name, sha, target, github["merged_prs"])
        merged = ancestry or proof is not None
        reasons: list[str] = []
        if not merged:
            reasons.append("tip is not proven merged into target")
        if is_keep_branch(name, keep_set, protected_set, github["default_branch"]):
            reasons.append("branch is protected by keep/default rules")
        if name in open_names:
            reasons.append("branch is the head of an open pull request")
        if only and name not in only:
            reasons.append("branch is outside --only-branch selection")
        row = {
            "name": name,
            "scope": scope,
            "sha": sha,
            "commit_time": timestamp,
            "stale": stale,
            "ancestor_of_target": ancestry,
            "merged_pr_merge_sha": proof,
            "eligible": False,
            "reasons": reasons,
        }
        if not reasons:
            row["eligible"] = True
        return row

    for name, sha in local_heads.items():
        result = branch_analysis(name, sha, "local")
        if name in checked_out:
            result["eligible"] = False
            result["reasons"].append("branch is checked out in a worktree")
        branch_rows.append(result)
        if result["eligible"]:
            actions.append({"type": "delete_local_branch", "branch": name, "sha": sha})

    for name, sha in remote_snapshot.items():
        result = branch_analysis(name, sha, "remote")
        # A protected remote branch is never a delete candidate even if GitHub
        # API output was incomplete; branch_analysis includes exact live names.
        branch_rows.append(result)
        if result["eligible"]:
            actions.append({"type": "delete_remote_branch", "branch": name, "sha": sha})

    primary_head = run_git(primary, "rev-parse", "HEAD")
    for entry in wt:
        path = Path(entry["path"])
        branch = entry.get("branch")
        detached_complete = entry.get("detached") and str(path) in explicit_completed
        # The primary tree is the anchor for backups/audit and is never removed.
        reasons: list[str] = []
        if path == primary:
            reasons.append("primary worktree")
        if entry.get("locked"):
            reasons.append("worktree is locked")
        if not branch and not detached_complete:
            reasons.append("detached snapshot has no explicit completion evidence")
        if branch and only and branch not in only:
            reasons.append("worktree branch is outside --only-branch selection")
        if branch:
            branch_sha = local_heads.get(branch)
            if branch_sha != entry.get("head"):
                reasons.append("worktree HEAD does not match its local branch tip")
            if (
                not branch_sha
                or not is_ancestor(top, branch_sha, target_sha)
                and not pr_merge_proof(top, branch, branch_sha, target, github["merged_prs"])
            ):
                reasons.append("worktree branch tip is not proven merged into target")
            if branch in open_names:
                reasons.append("worktree branch is the head of an open pull request")
            if is_keep_branch(branch, keep_set, protected_set, github["default_branch"]):
                reasons.append("worktree branch is protected by keep/default rules")
        if status_dirty(path):
            reasons.append("worktree has tracked or untracked changes")
        ignored = ignored_noncache(path)
        if ignored:
            reasons.append("ignored non-cache data requires archive before removal")
        if not proc_scan_complete:
            reasons.append("process cwd scan is incomplete")
        if any(is_within(Path(cwd), path) for cwd in proc_paths):
            reasons.append("a live process has cwd inside the worktree")
        if is_within(Path.cwd(), path):
            reasons.append("cleanup process cwd is inside the worktree")
        row = {
            "path": str(path),
            "branch": branch,
            "head": entry.get("head"),
            "locked": bool(entry.get("locked")),
            "detached": bool(entry.get("detached")),
            "dirty": status_dirty(path),
            "ignored_noncache": ignored,
            "eligible": not reasons,
            "reasons": reasons,
        }
        wt_rows.append(row)
        if row["eligible"]:
            actions.insert(
                0,
                {
                    "type": "remove_worktree",
                    "path": str(path),
                    "branch": branch,
                    "head": entry["head"],
                },
            )

    held_worktree_branches = {
        row["branch"] for row in wt_rows if row["branch"] and not row["eligible"]
    }
    if held_worktree_branches:
        for branch_row in branch_rows:
            if branch_row["name"] in held_worktree_branches:
                branch_row["eligible"] = False
                reason = "branch is retained by a preserved worktree"
                if reason not in branch_row["reasons"]:
                    branch_row["reasons"].append(reason)
        actions = [
            action
            for action in actions
            if not (
                action.get("branch") in held_worktree_branches
                and action["type"] in {"delete_local_branch", "delete_remote_branch"}
            )
        ]

    # A merged branch checked out only in a worktree scheduled for removal can
    # be deleted after that worktree. Keep it as a separate ordered action.
    safe_worktree_branches = {row["branch"] for row in wt_rows if row["eligible"] and row["branch"]}
    already_planned_local = {
        action["branch"] for action in actions if action["type"] == "delete_local_branch"
    }
    for row in branch_rows:
        if (
            row["scope"] == "local"
            and row["name"] in safe_worktree_branches
            and row["name"] not in already_planned_local
            and row["reasons"] == ["branch is checked out in a worktree"]
        ):
            row["eligible"] = True
            row["reasons"] = ["eligible after scheduled clean worktree removal"]
            actions.append(
                {"type": "delete_local_branch", "branch": row["name"], "sha": row["sha"]}
            )

    repo_plan = {
        "repo": str(top),
        "common_git_dir": str(common_dir),
        "remote": remote,
        "github_repo": github["slug"],
        "target": target,
        "target_sha": target_sha,
        "context_head": context_head,
        "primary_worktree": str(primary),
        "primary_head": primary_head,
        "default_branch": github["default_branch"],
        "protected_branches": github["protected"],
        "open_pr_heads": github["open_prs"],
        "remote_heads": remote_snapshot,
        "keep_branches": sorted(keep_set),
        "completed_worktrees": sorted(explicit_completed),
        "branches": branch_rows,
        "worktrees": wt_rows,
        "actions": actions,
        "audit_path": str(common_dir / "task-hygiene-audit.jsonl"),
        "plan_id": plan_id,
    }
    return repo_plan


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Build a multi-repository plan, failing closed on any evidence gap."""
    plan_id = str(uuid.uuid4())
    repos = [
        inspect_repo(
            repo,
            remote=args.remote,
            target=args.target,
            recent_days=args.recent_days,
            keep=args.keep,
            only_branches=args.only_branch,
            completed_worktrees=args.completed_worktree,
            plan_id=plan_id,
        )
        for repo in args.repo
    ]
    action_count = sum(len(item["actions"]) for item in repos)
    if action_count > args.max_actions:
        raise HygieneError(f"plan has {action_count} actions; cap is {args.max_actions}")
    plan = {
        "schema_version": VERSION,
        "plan_id": plan_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "policy": {
            "recent_days": args.recent_days,
            "remote": args.remote,
            "target": args.target,
            "max_actions": args.max_actions,
            "only_branches": args.only_branch,
            "keep": args.keep,
        },
        "repos": repos,
    }
    plan["plan_digest"] = plan_digest(plan)
    return plan


def plan_digest(plan: dict[str, Any]) -> str:
    """Bind apply actions to the exact reviewed plan contents."""
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    body = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def backup_ref(repo: Path, plan_id: str, scope: str, name: str, sha: str) -> str:
    """Create an immutable hidden backup ref with compare-and-create semantics."""
    token = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    ref = f"refs/task-archive/{plan_id}/{scope}/{token}"
    object_format = run_git(repo, "rev-parse", "--show-object-format")
    zero = "0" * (64 if object_format == "sha256" else 40)
    result = subprocess.run(
        ["git", "-C", str(repo), "update-ref", ref, sha, zero],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        existing = run_git(repo, "rev-parse", ref, check=False)
        if existing != sha:
            raise HygieneError("backup ref could not be created safely")
    return ref


def append_audit(path: Path, row: dict[str, Any]) -> None:
    """Append one compact JSON audit event without secrets or command output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_porcelain_worktrees(repo: Path) -> list[dict[str, Any]]:
    """Refresh worktree metadata before a destructive action."""
    return worktrees(repo)


def validate_live(
    repo_plan: dict[str, Any], *, include_github: bool
) -> tuple[bool, str, dict[str, Any] | None]:
    """Refresh fetch/remote/GitHub evidence and compare immutable plan anchors."""
    repo = Path(repo_plan["repo"])
    if run_git(repo, "rev-parse", "HEAD") != repo_plan["context_head"]:
        return False, "repository HEAD changed since plan", None
    try:
        heads = refresh_remote(repo, repo_plan["remote"])
        tracking = run_git(
            repo, "rev-parse", f"refs/remotes/{repo_plan['remote']}/{repo_plan['target']}"
        )
    except HygieneError as exc:
        return False, str(exc), None
    if (
        heads.get(repo_plan["target"]) != repo_plan["target_sha"]
        or tracking != repo_plan["target_sha"]
    ):
        return False, "target tip changed since plan", None
    planned_heads = repo_plan["remote_heads"]
    planned_actions = {
        action["branch"]: action["sha"]
        for action in repo_plan["actions"]
        if action["type"] == "delete_remote_branch"
    }
    for name in set(heads) | set(planned_heads):
        expected = planned_heads.get(name)
        actual = heads.get(name)
        if expected == actual:
            continue
        if name in planned_actions and actual is None:
            continue  # An absent planned head is a harmless idempotent skip.
        return False, "live remote heads changed since plan", None
    if not include_github:
        return True, "remote evidence unchanged", {"heads": heads}
    try:
        live = get_github_evidence(repo, repo_plan["remote"], repo_plan["target"])
    except HygieneError as exc:
        return False, str(exc), None
    if live["default_branch"] != repo_plan["default_branch"]:
        return False, "GitHub default branch changed since plan", None
    if live["protected"] != repo_plan["protected_branches"]:
        return False, "protected branch evidence changed since plan", None
    if live["open_prs"] != repo_plan["open_pr_heads"]:
        return False, "open pull request evidence changed since plan", None
    return True, "GitHub and remote evidence unchanged", {"heads": heads, "github": live}


def append_event(
    repo_plan: dict[str, Any],
    report: dict[str, Any],
    action: dict[str, Any],
    status: str,
    reason: str,
    **extra: Any,
) -> None:
    """Write both persistent audit and caller-selected apply report entries."""
    event = {
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "plan_id": repo_plan["plan_id"],
        "repo": repo_plan["repo"],
        "action": action,
        "status": status,
        "reason": reason,
        **extra,
    }
    append_audit(Path(repo_plan["audit_path"]), event)
    report["events"].append(event)


def apply_action(
    repo_plan: dict[str, Any],
    action: dict[str, Any],
    report: dict[str, Any],
    *,
    phase_evidence: dict[str, Any] | None,
) -> None:
    """Recheck exact action state, back it up, and apply one non-forced change."""
    repo = Path(repo_plan["repo"])
    action_type = action["type"]
    include_github = action_type == "delete_remote_branch"
    if run_git(repo, "rev-parse", "HEAD") != repo_plan["context_head"]:
        append_event(
            repo_plan,
            report,
            action,
            "skipped",
            "repository HEAD changed since plan",
            old_sha=action.get("sha", action.get("head")),
        )
        return
    if phase_evidence is None:
        okay, reason, phase_evidence = validate_live(repo_plan, include_github=include_github)
        if not okay:
            append_event(
                repo_plan,
                report,
                action,
                "skipped",
                reason,
                old_sha=action.get("sha", action.get("head")),
            )
            return

    try:
        if action_type == "remove_worktree":
            path = Path(action["path"])
            current = next(
                (row for row in read_porcelain_worktrees(repo) if row["path"] == str(path)), None
            )
            if current is None:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "worktree already absent",
                    old_sha=action.get("head"),
                )
                return
            if path == Path(repo_plan["primary_worktree"]) or current.get("locked"):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "primary or locked worktree",
                    old_sha=action.get("head"),
                )
                return
            if current.get("head") != action["head"] or current.get("branch") != action.get(
                "branch"
            ):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "worktree head or branch changed",
                    old_sha=action.get("head"),
                )
                return
            branch = action.get("branch")
            if branch:
                github = phase_evidence.get("github", {}) if phase_evidence else {}
                current_branch = run_git(
                    repo, "show-ref", "--verify", "--hash", f"refs/heads/{branch}", check=False
                )
                proof = pr_merge_proof(
                    repo, branch, action["head"], repo_plan["target"], github.get("merged_prs", [])
                )
                merged = (
                    is_ancestor(repo, action["head"], repo_plan["target_sha"]) or proof is not None
                )
                if (
                    current_branch != action["head"]
                    or not merged
                    or branch in {pr["head"] for pr in github.get("open_prs", [])}
                    or is_keep_branch(
                        branch,
                        set(repo_plan.get("keep_branches", [])),
                        set(github.get("protected", [])),
                        github.get("default_branch", ""),
                    )
                ):
                    append_event(
                        repo_plan,
                        report,
                        action,
                        "skipped",
                        "worktree branch is no longer safe to retire",
                        old_sha=action.get("head"),
                    )
                    return
            elif str(path) not in repo_plan.get("completed_worktrees", []):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "detached worktree lacks explicit completion evidence",
                    old_sha=action.get("head"),
                )
                return
            if status_dirty(path) or ignored_noncache(path):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "worktree became dirty or contains ignored data",
                    old_sha=action.get("head"),
                )
                return
            proc_paths, complete = process_cwds()
            if (
                not complete
                or is_within(Path.cwd(), path)
                or any(is_within(Path(cwd), path) for cwd in proc_paths)
            ):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "live process cwd or incomplete process scan",
                    old_sha=action.get("head"),
                )
                return
            if action.get("head"):
                backup = backup_ref(
                    repo, repo_plan["plan_id"], "worktree", str(path), action["head"]
                )
            else:
                backup = ""
            completed = subprocess.run(
                ["git", "-C", repo_plan["primary_worktree"], "worktree", "remove", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "git worktree remove refused removal",
                    old_sha=action.get("head"),
                    backup_ref=backup,
                )
                return
            append_event(
                repo_plan,
                report,
                action,
                "applied",
                "worktree removed",
                old_sha=action.get("head"),
                backup_ref=backup,
            )
            return

        branch = action["branch"]
        sha = action["sha"]
        if action_type == "delete_local_branch":
            current = run_git(
                repo, "show-ref", "--verify", "--hash", f"refs/heads/{branch}", check=False
            )
            if not current:
                append_event(
                    repo_plan, report, action, "skipped", "local branch already absent", old_sha=sha
                )
                return
            if current != sha:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "local branch tip changed",
                    old_sha=sha,
                    current_sha=current,
                )
                return
            if any(row.get("branch") == branch for row in read_porcelain_worktrees(repo)):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "branch is checked out in a worktree",
                    old_sha=sha,
                )
                return
            github = phase_evidence.get("github", {}) if phase_evidence else {}
            if branch in {pr["head"] for pr in github.get("open_prs", [])} or is_keep_branch(
                branch,
                set(repo_plan.get("keep_branches", [])),
                set(github.get("protected", [])),
                github.get("default_branch", ""),
            ):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "branch now has an open PR or keep/protection rule",
                    old_sha=sha,
                )
                return
            if not is_ancestor(repo, sha, repo_plan["target_sha"]):
                proof = (
                    pr_merge_proof(
                        repo,
                        branch,
                        sha,
                        repo_plan["target"],
                        phase_evidence.get("github", {}).get("merged_prs", []),
                    )
                    if phase_evidence
                    else None
                )
                if not proof:
                    # Local phase uses the initial PR evidence; re-read before
                    # each local deletion only if ancestry does not prove safety.
                    live = get_github_evidence(repo, repo_plan["remote"], repo_plan["target"])
                    proof = pr_merge_proof(
                        repo, branch, sha, repo_plan["target"], live["merged_prs"]
                    )
                    if not proof or not is_ancestor(repo, proof, repo_plan["target_sha"]):
                        append_event(
                            repo_plan,
                            report,
                            action,
                            "skipped",
                            "branch tip is no longer proven merged",
                            old_sha=sha,
                        )
                        return
            backup = backup_ref(repo, repo_plan["plan_id"], "local", branch, sha)
            result = subprocess.run(
                ["git", "-C", str(repo), "update-ref", "-d", f"refs/heads/{branch}", sha],
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "expected-SHA local deletion refused",
                    old_sha=sha,
                    backup_ref=backup,
                )
                return
            append_event(
                repo_plan,
                report,
                action,
                "applied",
                "merged local branch removed",
                old_sha=sha,
                backup_ref=backup,
            )
            return

        if action_type == "delete_remote_branch":
            current = phase_evidence["heads"].get(branch) if phase_evidence else None
            if current is None:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "remote branch already absent",
                    old_sha=sha,
                )
                return
            if current != sha:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "remote branch tip changed",
                    old_sha=sha,
                    current_sha=current,
                )
                return
            if any(row.get("branch") == branch for row in read_porcelain_worktrees(repo)):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "branch remains checked out in a worktree",
                    old_sha=sha,
                )
                return
            live = phase_evidence["github"]
            if branch in {row["head"] for row in live["open_prs"]} or is_keep_branch(
                branch,
                set(repo_plan.get("keep_branches", [])),
                set(live["protected"]),
                live["default_branch"],
            ):
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "branch now has an open PR or protection rule",
                    old_sha=sha,
                )
                return
            ancestor = is_ancestor(repo, sha, repo_plan["target_sha"])
            proof = (
                None
                if ancestor
                else pr_merge_proof(repo, branch, sha, repo_plan["target"], live["merged_prs"])
            )
            if not ancestor and not proof:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "remote branch tip is no longer proven merged",
                    old_sha=sha,
                )
                return
            backup = backup_ref(repo, repo_plan["plan_id"], "remote", branch, sha)
            lease_ref = f"refs/heads/{branch}:{sha}"
            pushed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "push",
                    f"--force-with-lease={lease_ref}",
                    repo_plan["remote"],
                    f":refs/heads/{branch}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if pushed.returncode != 0:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    "remote compare-and-swap deletion refused",
                    old_sha=sha,
                    backup_ref=backup,
                )
                return
            append_event(
                repo_plan,
                report,
                action,
                "applied",
                "merged remote branch removed with lease",
                old_sha=sha,
                backup_ref=backup,
            )
            return
        append_event(repo_plan, report, action, "skipped", "unknown action type", old_sha=sha)
    except HygieneError as exc:
        append_event(
            repo_plan,
            report,
            action,
            "skipped",
            str(exc),
            old_sha=action.get("sha", action.get("head")),
        )


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Apply safe worktree/local actions, then CAS-protected remote deletions."""
    if (
        plan.get("schema_version") != VERSION
        or not isinstance(plan.get("repos"), list)
        or plan.get("plan_digest") != plan_digest(plan)
    ):
        raise HygieneError("unsupported or malformed plan file")
    report: dict[str, Any] = {
        "schema_version": VERSION,
        "plan_id": plan.get("plan_id"),
        "events": [],
    }
    for repo_plan in plan["repos"]:
        actions = repo_plan.get("actions", [])
        local_actions = [item for item in actions if item["type"] != "delete_remote_branch"]
        remote_actions = [item for item in actions if item["type"] == "delete_remote_branch"]

        # One fresh repository/GitHub snapshot gates the local batch; exact SHA,
        # dirty/worktree state and process cwd are still checked per action.
        phase_ok, phase_reason, phase_evidence = validate_live(repo_plan, include_github=True)
        for action in local_actions:
            if not phase_ok:
                append_event(
                    repo_plan,
                    report,
                    action,
                    "skipped",
                    phase_reason,
                    old_sha=action.get("sha", action.get("head")),
                )
                continue
            apply_action(repo_plan, action, report, phase_evidence=phase_evidence)

        for offset in range(0, len(remote_actions), REMOTE_REVALIDATION_BATCH):
            batch = remote_actions[offset : offset + REMOTE_REVALIDATION_BATCH]
            # Refresh live PR/protection/head evidence before each bounded batch.
            # Each actual delete still uses a per-ref expected-SHA lease.
            okay, reason, evidence = validate_live(repo_plan, include_github=True)
            for action in batch:
                if not okay:
                    append_event(
                        repo_plan, report, action, "skipped", reason, old_sha=action.get("sha")
                    )
                    continue
                apply_action(repo_plan, action, report, phase_evidence=evidence)
    return report


def write_json(path: Path, value: Any) -> None:
    """Write deterministic, reviewable JSON via an atomic sibling replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def add_inventory_options(parser: argparse.ArgumentParser) -> None:
    """Declare shared plan/status arguments."""
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="repository path; repeat for multiple repositories",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--target", default="develop", help="target branch name on --remote")
    parser.add_argument("--recent-days", type=int, default=DEFAULT_RECENT_DAYS)
    parser.add_argument(
        "--keep",
        "--keep-branch",
        dest="keep",
        action="append",
        default=[],
        help="additional local/remote branch name to keep",
    )
    parser.add_argument(
        "--only-branch",
        action="append",
        default=[],
        help="limit actions to this exact branch name; repeatable",
    )
    parser.add_argument(
        "--completed-worktree",
        action="append",
        default=[],
        help="explicitly owned and completed detached worktree path",
    )
    parser.add_argument("--max-actions", type=int, default=MAX_ACTIONS)


def parser_for_cli() -> argparse.ArgumentParser:
    """Build plan/status/apply commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="save a read-only cleanup plan")
    add_inventory_options(plan_parser)
    plan_parser.add_argument(
        "--output", required=True, help="saved JSON plan path, outside candidate worktrees"
    )
    status_parser = subparsers.add_parser("status", help="show read-only live inventory")
    add_inventory_options(status_parser)
    apply_parser = subparsers.add_parser("apply", help="apply a previously reviewed plan")
    apply_parser.add_argument("--planfile", required=True)
    apply_parser.add_argument("--apply-report", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return an appropriate process exit code."""
    args = parser_for_cli().parse_args(argv)
    try:
        if args.command in {"plan", "status"}:
            if args.recent_days < 0 or args.max_actions < 0 or args.max_actions > MAX_ACTIONS:
                raise HygieneError(
                    f"--recent-days must be nonnegative and --max-actions must be 0..{MAX_ACTIONS}"
                )
            plan = build_plan(args)
            if args.command == "plan":
                output = Path(args.output).expanduser().resolve()
                for item in plan["repos"]:
                    if any(
                        is_within(output, Path(row["path"]))
                        for row in item["worktrees"]
                        if row["eligible"]
                    ):
                        raise HygieneError("plan output must be outside candidate worktrees")
                write_json(output, plan)
                for item in plan["repos"]:
                    for action in item["actions"]:
                        append_audit(
                            Path(item["audit_path"]),
                            {
                                "at": dt.datetime.now(dt.UTC).isoformat(),
                                "plan_id": plan["plan_id"],
                                "repo": item["repo"],
                                "action": action,
                                "status": "planned",
                                "reason": "eligible under saved plan evidence",
                                "old_sha": action.get("sha", action.get("head")),
                            },
                        )
                print(
                    json.dumps(
                        {
                            "plan": str(output),
                            "plan_id": plan["plan_id"],
                            "actions": sum(len(item["actions"]) for item in plan["repos"]),
                        },
                        indent=2,
                    )
                )
            else:
                print(json.dumps(plan, indent=2, ensure_ascii=False))
            return 0
        plan_path = Path(args.planfile).expanduser().resolve()
        report_path = Path(args.apply_report).expanduser().resolve()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for item in plan.get("repos", []):
            candidates = [
                Path(action["path"])
                for action in item.get("actions", [])
                if action.get("type") == "remove_worktree"
            ]
            if any(
                is_within(plan_path, path) or is_within(report_path, path) for path in candidates
            ):
                raise HygieneError("plan and apply report must be outside candidate worktrees")
        report = apply_plan(plan)
        write_json(report_path, report)
        applied = sum(event["status"] == "applied" for event in report["events"])
        skipped = sum(event["status"] == "skipped" for event in report["events"])
        print(
            json.dumps(
                {
                    "apply_report": str(Path(args.apply_report).resolve()),
                    "applied": applied,
                    "skipped": skipped,
                },
                indent=2,
            )
        )
        blocking_skipped = [
            event
            for event in report["events"]
            if event["status"] == "skipped" and "already absent" not in event["reason"]
        ]
        return 0 if not blocking_skipped else 2
    except (HygieneError, OSError, json.JSONDecodeError) as exc:
        print(f"task-hygiene: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
