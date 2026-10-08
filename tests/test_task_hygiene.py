"""Disposable real-Git tests for task branch/worktree cleanup safety.

The fixtures use a local bare remote and an isolated mock gh executable; no
Cyrene repository or real GitHub state is modified. 测试只操作临时仓库。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "task_hygiene.py"
SPEC = importlib.util.spec_from_file_location("task_hygiene", SCRIPT)
assert SPEC and SPEC.loader
task_hygiene = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = task_hygiene
SPEC.loader.exec_module(task_hygiene)


MOCK_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if os.environ.get('TASK_HYGIENE_GH_FAIL'):
    raise SystemExit(1)
fixture = json.load(open(os.environ['TASK_HYGIENE_GH_FIXTURE'], encoding='utf-8'))
if args[0] == 'api':
    endpoint = args[-1]
    if endpoint == 'repos/owner/project':
        print(json.dumps({'default_branch': fixture.get('default_branch', 'develop')}))
    elif '/pulls?' in endpoint:
        rows = fixture.get('open_prs', [])
        for row in rows:
            print('\\t'.join([row['head'], row['sha'], row['base'], row.get('repository', 'owner/project')]))
    elif '/branches?' in endpoint:
        for name in fixture.get('protected', ['develop']):
            print(name)
    else:
        raise SystemExit(2)
elif args[0] == 'pr' and args[1] == 'list':
    print(json.dumps(fixture.get('merged_prs', [])))
else:
    raise SystemExit(2)
"""


class GitSandbox:
    """Create isolated GitHub-shaped remotes and run the task-hygiene CLI."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.repo = tmp_path / "repo"
        self.bare = tmp_path / "origin.git"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(MOCK_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.fixture_path = tmp_path / "gh-fixture.json"
        self.fixture: dict[str, Any] = {
            "default_branch": "develop",
            "protected": ["develop"],
            "open_prs": [],
            "merged_prs": [],
        }
        self.write_fixture()
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        self.env["TASK_HYGIENE_GH_FIXTURE"] = str(self.fixture_path)
        self.env.pop("TASK_HYGIENE_GH_FAIL", None)
        self.env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text(
            f'[url "{self.bare.as_uri()}"]\n\tinsteadOf = git@github.com:owner/project.git\n',
            encoding="utf-8",
        )
        self.git(self.root, "init", "--bare", str(self.bare))
        self.git(self.root, "init", "--initial-branch=develop", str(self.repo))
        self.git(self.repo, "config", "user.name", "Task Hygiene Test")
        self.git(self.repo, "config", "user.email", "task-hygiene@example.invalid")
        self.git(self.repo, "remote", "add", "origin", "git@github.com:owner/project.git")
        (self.repo / ".gitignore").write_text("*.secret\n", encoding="utf-8")
        (self.repo / "data.txt").write_text("base\n", encoding="utf-8")
        self.commit(self.repo, "initial", ancient=True)
        self.git(self.repo, "push", "-u", "origin", "develop")

    def git(self, cwd: Path, *args: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if check and completed.returncode:
            raise AssertionError(f"git {args!r} failed: {completed.stderr}")
        return completed.stdout.strip()

    def commit(self, cwd: Path, message: str, *, ancient: bool = False) -> str:
        date = "2020-01-01T00:00:00+00:00" if ancient else "2026-10-08T12:00:00+00:00"
        commit_env = self.env | {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        self.git(cwd, "add", "-A")
        completed = subprocess.run(
            ["git", "-C", str(cwd), "commit", "-m", message],
            env=commit_env,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise AssertionError(f"git commit failed: {completed.stderr}")
        return self.git(cwd, "rev-parse", "HEAD")

    def add_branch(
        self, name: str, *, merged: bool = False, push: bool = True, recent: bool = False
    ) -> str:
        self.git(self.repo, "switch", "-c", name, "develop")
        if not merged:
            (self.repo / f"{name}.txt").write_text(f"branch {name}\n", encoding="utf-8")
            sha = self.commit(self.repo, name, ancient=not recent)
        else:
            sha = self.git(self.repo, "rev-parse", "HEAD")
        if push:
            self.git(self.repo, "push", "origin", name)
        self.git(self.repo, "switch", "develop")
        return sha

    def add_worktree(
        self, name: str, *, dirty: bool = False, lock: bool = False, ignored: bool = False
    ) -> Path:
        self.git(self.repo, "branch", name, "develop")
        path = self.root / "worktrees" / name
        path.parent.mkdir(exist_ok=True)
        self.git(self.repo, "worktree", "add", str(path), name)
        if dirty:
            (path / "data.txt").write_text("edited\n", encoding="utf-8")
        if ignored:
            (path / "important.secret").write_text("keep me\n", encoding="utf-8")
        if lock:
            self.git(self.repo, "worktree", "lock", str(path), "--reason", "fixture lock")
        return path

    def write_fixture(self) -> None:
        self.fixture_path.write_text(json.dumps(self.fixture), encoding="utf-8")

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def plan(self, output: Path, *extra: str) -> dict[str, Any]:
        result = self.cli(
            "plan",
            "--repo",
            str(self.repo),
            "--output",
            str(output),
            *extra,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(output.read_text(encoding="utf-8"))

    def apply(self, plan_path: Path, report_path: Path) -> tuple[int, dict[str, Any]]:
        result = self.cli(
            "apply",
            "--planfile",
            str(plan_path),
            "--apply-report",
            str(report_path),
        )
        assert report_path.exists(), result.stderr
        return result.returncode, json.loads(report_path.read_text(encoding="utf-8"))


def actions_for(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return plan["repos"][0]["actions"]


def row_for(plan: dict[str, Any], scope: str, name: str) -> dict[str, Any]:
    return next(
        row for row in plan["repos"][0]["branches"] if row["scope"] == scope and row["name"] == name
    )


def test_only_stale_merged_local_and_remote_heads_are_planned(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sandbox.add_branch("old-merged", merged=True)
    sandbox.add_branch("old-unmerged")
    plan = sandbox.plan(tmp_path / "plan.json", "--only-branch", "old-merged")
    assert {(a["type"], a.get("branch")) for a in actions_for(plan)} == {
        ("delete_local_branch", "old-merged"),
        ("delete_remote_branch", "old-merged"),
    }
    unmerged = row_for(plan, "remote", "old-unmerged")
    assert unmerged["stale"] is True
    assert unmerged["eligible"] is False
    assert "not proven merged" in " ".join(unmerged["reasons"])


def test_recent_exact_merged_tips_remain_cleanup_eligible(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sha = sandbox.add_branch("recent-merged", recent=True)
    sandbox.git(sandbox.repo, "merge", "--ff-only", "recent-merged")
    sandbox.git(sandbox.repo, "push", "origin", "develop")
    plan = sandbox.plan(tmp_path / "plan.json", "--only-branch", "recent-merged")
    assert row_for(plan, "remote", "recent-merged")["stale"] is False
    assert row_for(plan, "remote", "recent-merged")["eligible"] is True
    assert any(action["sha"] == sha for action in actions_for(plan))


def test_safe_merged_deletion_has_backup_refs_and_repeat_is_harmless(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    old_sha = sandbox.add_branch("old-merged", merged=True)
    plan_path = tmp_path / "plan.json"
    plan = sandbox.plan(plan_path, "--only-branch", "old-merged")
    code, report = sandbox.apply(plan_path, tmp_path / "apply.json")
    assert code == 0
    assert all(event["status"] == "applied" for event in report["events"])
    assert not sandbox.git(
        sandbox.repo, "show-ref", "--verify", "refs/heads/old-merged", check=False
    )
    assert "old-merged" not in sandbox.git(sandbox.repo, "ls-remote", "--heads", "origin")
    refs = sandbox.git(
        sandbox.repo,
        "for-each-ref",
        "--format=%(objectname)",
        f"refs/task-archive/{plan['plan_id']}",
    )
    assert refs.splitlines() == [old_sha, old_sha]

    repeat_code, repeat = sandbox.apply(plan_path, tmp_path / "repeat.json")
    assert repeat_code == 0
    assert len(repeat["events"]) == 2
    assert all("already absent" in event["reason"] for event in repeat["events"])


@pytest.mark.parametrize("kind", ["dirty", "locked", "ignored"])
def test_worktree_safety_conditions_preserve_candidate(tmp_path: Path, kind: str) -> None:
    sandbox = GitSandbox(tmp_path)
    path = sandbox.add_worktree(
        f"work-{kind}",
        dirty=kind == "dirty",
        lock=kind == "locked",
        ignored=kind == "ignored",
    )
    plan = sandbox.plan(tmp_path / "plan.json", "--only-branch", f"work-{kind}")
    row = next(item for item in plan["repos"][0]["worktrees"] if item["path"] == str(path))
    assert row["eligible"] is False
    assert not any(action.get("path") == str(path) for action in actions_for(plan))
    if kind == "ignored":
        assert row["ignored_noncache"] == ["important.secret"]


def test_primary_worktree_is_never_a_removal_candidate(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    plan = sandbox.plan(tmp_path / "plan.json")
    primary = next(row for row in plan["repos"][0]["worktrees"] if row["path"] == str(sandbox.repo))
    assert "primary worktree" in primary["reasons"]
    assert not any(
        action["type"] == "remove_worktree" and action["path"] == str(sandbox.repo)
        for action in actions_for(plan)
    )


def test_clean_merged_worktree_is_removed_before_its_branch_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = GitSandbox(tmp_path)
    path = sandbox.add_worktree("old-worktree")
    sandbox.git(sandbox.repo, "push", "origin", "old-worktree")
    plan_path = tmp_path / "plan.json"
    args = task_hygiene.parser_for_cli().parse_args(
        [
            "plan",
            "--repo",
            str(sandbox.repo),
            "--output",
            str(plan_path),
            "--only-branch",
            "old-worktree",
        ]
    )
    monkeypatch.setattr(task_hygiene, "process_cwds", lambda: ([], True))
    original_run = subprocess.run

    def run_with_fixture_env(*call_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        kwargs.setdefault("env", sandbox.env)
        return original_run(*call_args, **kwargs)

    monkeypatch.setattr(task_hygiene.subprocess, "run", run_with_fixture_env)
    plan = task_hygiene.build_plan(args)
    task_hygiene.write_json(plan_path, plan)
    actions = actions_for(plan)
    assert actions[0]["type"] == "remove_worktree"
    assert actions[0]["path"] == str(path)
    assert any(action["type"] == "delete_local_branch" for action in actions)
    assert any(action["type"] == "delete_remote_branch" for action in actions)

    report = task_hygiene.apply_plan(plan)
    assert all(event["status"] == "applied" for event in report["events"])
    assert not path.exists()
    assert not sandbox.git(
        sandbox.repo, "show-ref", "--verify", "refs/heads/old-worktree", check=False
    )
    assert "old-worktree" not in sandbox.git(sandbox.repo, "ls-remote", "--heads", "origin")
    assert [event["status"] for event in report["events"]] == ["applied", "applied", "applied"]


def test_worktree_dirtied_after_plan_blocks_later_remote_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = GitSandbox(tmp_path)
    path = sandbox.add_worktree("changed-after-plan")
    sandbox.git(sandbox.repo, "push", "origin", "changed-after-plan")
    args = task_hygiene.parser_for_cli().parse_args(
        [
            "plan",
            "--repo",
            str(sandbox.repo),
            "--output",
            str(tmp_path / "plan.json"),
            "--only-branch",
            "changed-after-plan",
        ]
    )
    monkeypatch.setattr(task_hygiene, "process_cwds", lambda: ([], True))
    original_run = subprocess.run

    def run_with_fixture_env(*call_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        kwargs.setdefault("env", sandbox.env)
        return original_run(*call_args, **kwargs)

    monkeypatch.setattr(task_hygiene.subprocess, "run", run_with_fixture_env)
    plan = task_hygiene.build_plan(args)
    assert any(action["type"] == "remove_worktree" for action in actions_for(plan))
    (path / "data.txt").write_text("new work in progress\n", encoding="utf-8")

    report = task_hygiene.apply_plan(plan)
    remote = next(
        event for event in report["events"] if event["action"]["type"] == "delete_remote_branch"
    )
    assert remote["status"] == "skipped"
    assert "checked out in a worktree" in remote["reason"]
    assert "changed-after-plan" in sandbox.git(sandbox.repo, "ls-remote", "--heads", "origin")


def test_open_pr_branch_is_preserved_even_when_tip_is_ancestor(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sha = sandbox.add_branch("open-head", merged=True)
    sandbox.fixture["open_prs"] = [{"head": "open-head", "sha": sha, "base": "develop"}]
    sandbox.write_fixture()
    plan = sandbox.plan(tmp_path / "plan.json", "--only-branch", "open-head")
    assert not actions_for(plan)
    assert row_for(plan, "remote", "open-head")["eligible"] is False


def test_live_process_worktree_holds_both_local_and_remote_branch(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    path = sandbox.add_worktree("active-session")
    sandbox.git(sandbox.repo, "push", "origin", "active-session")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys,time; os.chdir(sys.argv[1]); time.sleep(60)",
            str(path),
        ],
        env=sandbox.env,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            cwd_link = Path(f"/proc/{process.pid}/cwd")
            if cwd_link.exists() and cwd_link.resolve() == path:
                break
            time.sleep(0.02)
        plan = sandbox.plan(tmp_path / "plan.json", "--only-branch", "active-session")
        row = next(item for item in plan["repos"][0]["worktrees"] if item["path"] == str(path))
        assert "a live process has cwd inside the worktree" in row["reasons"]
        assert not any(action.get("branch") == "active-session" for action in actions_for(plan))
        for scope in ("local", "remote"):
            branch = row_for(plan, scope, "active-session")
            assert branch["eligible"] is False
            assert "branch is retained by a preserved worktree" in branch["reasons"]
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_github_evidence_unavailable_fails_closed(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sandbox.add_branch("old-merged", merged=True)
    sandbox.env["TASK_HYGIENE_GH_FAIL"] = "1"
    output = tmp_path / "plan.json"
    result = sandbox.cli("plan", "--repo", str(sandbox.repo), "--output", str(output))
    assert result.returncode == 2
    assert not output.exists()


def test_target_advancement_after_plan_skips_all_ref_deletions(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sandbox.add_branch("old-merged", merged=True)
    plan_path = tmp_path / "plan.json"
    sandbox.plan(plan_path, "--only-branch", "old-merged")
    sandbox.git(sandbox.repo, "switch", "develop")
    (sandbox.repo / "target-advance.txt").write_text("new target\n", encoding="utf-8")
    target_sha = sandbox.commit(sandbox.repo, "advance target")
    sandbox.git(sandbox.repo, "push", "origin", "develop")
    code, report = sandbox.apply(plan_path, tmp_path / "apply.json")
    assert code == 2
    assert all(event["status"] == "skipped" for event in report["events"])
    assert sandbox.git(sandbox.repo, "rev-parse", "refs/heads/old-merged")
    assert sandbox.git(sandbox.repo, "ls-remote", "--heads", "origin", "old-merged")
    assert target_sha == sandbox.git(sandbox.repo, "rev-parse", "develop")


def test_new_local_branch_tip_since_plan_skips_deletion(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    old_sha = sandbox.add_branch("old-local", merged=True, push=False)
    plan_path = tmp_path / "plan.json"
    sandbox.plan(plan_path, "--only-branch", "old-local")
    sandbox.git(sandbox.repo, "switch", "old-local")
    (sandbox.repo / "later.txt").write_text("new local tip\n", encoding="utf-8")
    new_sha = sandbox.commit(sandbox.repo, "advance local branch", ancient=True)
    sandbox.git(sandbox.repo, "switch", "develop")
    code, report = sandbox.apply(plan_path, tmp_path / "apply.json")
    assert code == 2
    local = next(
        event for event in report["events"] if event["action"]["type"] == "delete_local_branch"
    )
    assert local["status"] == "skipped"
    assert local["old_sha"] == old_sha
    assert sandbox.git(sandbox.repo, "rev-parse", "refs/heads/old-local") == new_sha


def test_open_pr_change_after_plan_skips_local_and_remote_deletion(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sha = sandbox.add_branch("old-merged", merged=True)
    plan_path = tmp_path / "plan.json"
    sandbox.plan(plan_path, "--only-branch", "old-merged")
    sandbox.fixture["open_prs"] = [{"head": "old-merged", "sha": sha, "base": "develop"}]
    sandbox.write_fixture()
    code, report = sandbox.apply(plan_path, tmp_path / "apply.json")
    assert code == 2
    assert all(event["status"] == "skipped" for event in report["events"])
    assert sandbox.git(sandbox.repo, "rev-parse", "refs/heads/old-merged") == sha
    assert "old-merged" in sandbox.git(sandbox.repo, "ls-remote", "--heads", "origin")


def test_remote_compare_and_swap_lease_protects_a_changed_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = GitSandbox(tmp_path)
    original_sha = sandbox.add_branch("old-merged", merged=True)
    other_sha = sandbox.add_branch("other-tip")
    plan_path = tmp_path / "plan.json"
    sandbox.plan(plan_path, "--only-branch", "old-merged")
    real_run = subprocess.run
    changed = False

    def race_before_push(
        command: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal changed
        if not changed and len(command) > 3 and command[0] == "git" and "push" in command:
            real_run(
                [
                    "git",
                    "--git-dir",
                    str(sandbox.bare),
                    "update-ref",
                    "refs/heads/old-merged",
                    other_sha,
                ],
                check=True,
                env=sandbox.env,
            )
            changed = True
        kwargs.setdefault("env", sandbox.env)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(task_hygiene.subprocess, "run", race_before_push)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    report = task_hygiene.apply_plan(plan)
    assert changed is True, report
    remote = next(
        event for event in report["events"] if event["action"]["type"] == "delete_remote_branch"
    )
    assert remote["status"] == "skipped"
    assert "compare-and-swap" in remote["reason"]
    assert sandbox.git(sandbox.repo, "ls-remote", "--heads", "origin").splitlines()
    assert original_sha != other_sha


def test_plan_command_accepts_branch_filter_and_status_is_read_only_inventory(
    tmp_path: Path,
) -> None:
    sandbox = GitSandbox(tmp_path)
    sandbox.add_branch("old-merged", merged=True)
    output = tmp_path / "plan.json"
    plan = sandbox.plan(output, "--only-branch", "old-merged")
    status = sandbox.cli("status", "--repo", str(sandbox.repo), "--only-branch", "old-merged")
    assert status.returncode == 0, status.stderr
    status_data = json.loads(status.stdout)
    assert status_data["plan_id"] != plan["plan_id"]
    assert output.exists()


def test_plan_digest_rejects_edited_actions(tmp_path: Path) -> None:
    sandbox = GitSandbox(tmp_path)
    sandbox.add_branch("old-merged", merged=True)
    plan = sandbox.plan(tmp_path / "plan.json", "--only-branch", "old-merged")
    plan["repos"][0]["actions"][0]["sha"] = "0" * 40
    with pytest.raises(task_hygiene.HygieneError, match="malformed plan"):
        task_hygiene.apply_plan(plan)
