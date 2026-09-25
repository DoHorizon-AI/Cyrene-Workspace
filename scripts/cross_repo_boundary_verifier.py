#!/usr/bin/env python3
"""
┌───────────────────────────────────────────────────────────────────────────────┐
│  📄 cross_repo_boundary_verifier.py                                            │
│  Module: scripts.cross_repo_boundary_verifier                                 │
│  Role: Enforces Platform exact policy, API Naming Constitution, and boundary    │
│        rules across all Product service repositories.                          │
│                                                                               │
│  模块职责：从 Cyrene-Platform 的 exact Git SHA 动态加载权威政策，并在各业务服务    │
│  （Reactor、Yield、Exchange、Catalyst、Echo、Navigator）源码上执行命名与边界门禁。   │
└───────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLATFORM_REL_PATH = Path("../Cyrene-Platform")
EXCLUDED_PATTERNS = (
    "*.md",
    "*.txt",
    "*.json",
    "*.lock",
    "*.toml",
    "*.yaml",
    "*.yml",
    "**/tests/**",
    "**/fixtures/**",
    "**/target/**",
    "**/build/**",
    "**/dist/**",
    "**/.venv/**",
    "**/node_modules/**",
    "**/bin/**",
    "**/obj/**",
)
CODE_EXTENSIONS = (
    ".py",
    ".rs",
    ".kt",
    ".java",
    ".ts",
    ".tsx",
    ".js",
    ".cs",
    ".proto",
    ".go",
)


@dataclass(frozen=True)
class PolicyAuthority:
    """Exact Platform authority metadata and machine-readable policy.

    中文:Platform 精确版本的权威 metadata 和机器可读 policy。
    """

    platform_path: Path
    exact_sha: str
    policy_version: int
    canonical_document: str
    forbidden_symbols: list[str]
    forbidden_patterns: list[tuple[str, re.Pattern[str]]] = field(
        default_factory=list, hash=False, compare=False
    )

    @classmethod
    def load(cls, platform_path: Path) -> PolicyAuthority:
        """Resolve exact git SHA of Platform and load api-naming policy.

        中文:解析 Platform 的精确 git SHA,并读取 api-naming policy。
        """
        if not platform_path.is_dir():
            raise FileNotFoundError(f"Platform directory not found: {platform_path}")

        try:
            exact_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=platform_path,
                text=True,
            ).strip()
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"Failed to query git SHA from {platform_path}: {exc}") from exc

        try:
            raw_policy = subprocess.check_output(
                ["git", "show", f"{exact_sha}:tooling/architecture/api-naming.toml"],
                cwd=platform_path,
                text=True,
            )
        except subprocess.SubprocessError:
            local_fallback = platform_path / "tooling/architecture/api-naming.toml"
            if not local_fallback.is_file():
                raise FileNotFoundError(
                    f"api-naming.toml not found at {exact_sha} or on disk in {platform_path}"
                )
            raw_policy = local_fallback.read_text(encoding="utf-8")

        policy_dict = tomllib.loads(raw_policy)
        forbidden_list = policy_dict.get("forbidden", {}).get("symbols", [])
        patterns = [
            (
                sym,
                re.compile(rf"(?<![A-Za-z0-9_]){re.escape(sym)}(?![A-Za-z0-9_])"),
            )
            for sym in forbidden_list
        ]

        return cls(
            platform_path=platform_path,
            exact_sha=exact_sha,
            policy_version=policy_dict.get("version", 1),
            canonical_document=policy_dict.get("canonical_document", ""),
            forbidden_symbols=forbidden_list,
            forbidden_patterns=patterns,
        )


@dataclass
class Violation:
    repository: str
    rule_category: str
    file_path: str
    line_number: int
    rule_name: str
    detail: str
    offending_line: str


@dataclass
class VerificationResult:
    platform_exact_sha: str
    policy_version: int
    forbidden_symbols_count: int
    repositories_checked: list[str]
    total_files_scanned: int
    passed: bool
    violations: list[Violation] = field(default_factory=list)


def is_path_excluded(path_str: str, excluded_globs: Iterable[str]) -> bool:
    """Check if file matches any excluded glob pattern.

    中文:检查文件路径是否匹配任一排除 glob pattern。
    """
    return any(fnmatch.fnmatch(path_str, pat) for pat in excluded_globs)


class CrossRepoBoundaryVerifier:
    """Verifies Product repositories against exact Platform governance policy.

    中文:依据 Platform 精确版本的治理 policy 检查 Product 仓库。
    """

    def __init__(
        self,
        workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
        platform_path: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        resolved_platform = (
            (self.workspace_root / platform_path).resolve()
            if platform_path
            else (self.workspace_root / DEFAULT_PLATFORM_REL_PATH).resolve()
        )
        self.authority = PolicyAuthority.load(resolved_platform)
        self.manifest_path = self.workspace_root / "repositories.yaml"

    def _load_repositories_manifest(self) -> list[dict[str, Any]]:
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"repositories.yaml not found at {self.manifest_path}")
        with open(self.manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        return manifest.get("repositories", [])

    def verify(
        self,
        target_repos: list[str] | None = None,
        include_tests: bool = False,
    ) -> VerificationResult:
        """Run naming and boundary checks against targeted Product repositories.

        中文:对指定 Product 仓库运行命名和边界检查。
        """
        repo_configs = self._load_repositories_manifest()

        # Default to 6 Product Services
        # 中文:默认检查六个 Product Services。
        default_target_names = {
            "Cyrene-Reactor",
            "Cyrene-Yield",
            "Cyrene-Exchange",
            "Cyrene-Catalyst",
            "Cyrene-Echo",
            "Cyrene-Navigator",
        }
        active_names = set(target_repos) if target_repos else default_target_names

        violations: list[Violation] = []
        repositories_checked: list[str] = []
        total_files = 0

        # Boundary regex guards
        # 中文:边界正则保护规则。
        kernel_daemon_direct_import = re.compile(
            r"(?:from\s+cy_kernel_daemon\b|use\s+cy_kernel_daemon::|import\s+.*cy_kernel_daemon)"
        )

        for repo_entry in repo_configs:
            repo_name = repo_entry.get("name", "")
            if repo_name not in active_names:
                continue

            repositories_checked.append(repo_name)
            repo_path = (self.workspace_root / repo_entry.get("path", "")).resolve()

            # Rule 1: Check prohibited retired files (e.g. service.json in Navigator)
            # 中文:规则 1:检查已禁用的退役文件(例如 Navigator 中的 service.json)
            # 中文:规则 1:检查已退役且禁止出现的文件,例如 Navigator 中的 service.json。
            if repo_name == "Cyrene-Navigator":
                legacy_service_json = repo_path / "service.json"
                if legacy_service_json.exists():
                    violations.append(
                        Violation(
                            repository=repo_name,
                            rule_category="boundary_guard",
                            file_path=str(legacy_service_json.relative_to(repo_path)),
                            line_number=1,
                            rule_name="prohibited_legacy_file",
                            detail="Retired service.json must not be present in Navigator",
                            offending_line="<file exists>",
                        )
                    )

            # Gather source roots
            # 中文:收集源码根目录。
            pointers = repo_entry.get("pointers", {})
            source_roots: list[str] = pointers.get("primary_source_roots", [])
            api_roots: list[str] = pointers.get("api_contract_roots", [])
            all_candidate_roots = list(source_roots) + list(api_roots)

            for root_rel in all_candidate_roots:
                resolved_root = (self.workspace_root / root_rel).resolve()
                if not resolved_root.exists():
                    continue

                for dirpath, _, filenames in os.walk(resolved_root):
                    for filename in filenames:
                        if not any(filename.endswith(ext) for ext in CODE_EXTENSIONS):
                            continue

                        file_path = Path(dirpath) / filename
                        rel_file_str = str(file_path.relative_to(self.workspace_root.parent))

                        globs_to_exclude = list(EXCLUDED_PATTERNS)
                        if include_tests:
                            globs_to_exclude = [g for g in globs_to_exclude if "tests" not in g]

                        if is_path_excluded(rel_file_str, globs_to_exclude):
                            continue

                        total_files += 1

                        try:
                            content = file_path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            continue

                        for line_idx, line in enumerate(content.splitlines(), 1):
                            # Naming Constitution Check: Forbidden symbols from Platform authority
                            # 中文:命名章程检查:禁止出现属于 Platform 权威范围的符号
                            # 中文:Naming Constitution 检查:Platform authority 禁止的符号。
                            for sym, pattern in self.authority.forbidden_patterns:
                                if pattern.search(line):
                                    violations.append(
                                        Violation(
                                            repository=repo_name,
                                            rule_category="api_naming_constitution",
                                            file_path=rel_file_str,
                                            line_number=line_idx,
                                            rule_name=sym,
                                            detail=(
                                                f"Forbidden legacy symbol '{sym}' referenced. "
                                                f"See Platform {self.authority.canonical_document}"
                                            ),
                                            offending_line=line.strip(),
                                        )
                                    )

                            # Boundary Check: Forbidden direct Kernel daemon internal import
                            # 中文:边界检查:禁止直接导入 Kernel daemon 内部模块
                            # 中文:边界检查:禁止直接导入 Kernel daemon 内部实现。
                            if kernel_daemon_direct_import.search(line):
                                violations.append(
                                    Violation(
                                        repository=repo_name,
                                        rule_category="boundary_guard",
                                        file_path=rel_file_str,
                                        line_number=line_idx,
                                        rule_name="kernel_daemon_direct_coupling",
                                        detail=(
                                            "Product service directly coupled to private Platform "
                                            "cy_kernel_daemon source."
                                        ),
                                        offending_line=line.strip(),
                                    )
                                )

        return VerificationResult(
            platform_exact_sha=self.authority.exact_sha,
            policy_version=self.authority.policy_version,
            forbidden_symbols_count=len(self.authority.forbidden_symbols),
            repositories_checked=repositories_checked,
            total_files_scanned=total_files,
            passed=(len(violations) == 0),
            violations=violations,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cross-repository naming constitution and boundary verifier"
    )
    parser.add_argument(
        "--platform-path",
        type=Path,
        default=DEFAULT_PLATFORM_REL_PATH,
        help="Path to Cyrene-Platform repository",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output formatting",
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        default=None,
        help="Specific repositories to verify (defaults to 6 product services)",
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Also scan test files in source roots",
    )
    args = parser.parse_args()

    try:
        verifier = CrossRepoBoundaryVerifier(
            workspace_root=DEFAULT_WORKSPACE_ROOT,
            platform_path=args.platform_path,
        )
        result = verifier.verify(
            target_repos=args.repos,
            include_tests=args.include_tests,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
    ) as exc:
        print(f"[FATAL] Failed to initialize cross-repo verifier: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        res_dict = asdict(result)
        print(json.dumps(res_dict, indent=2))
    else:
        print("=" * 80)
        print("  CYRENE CROSS-REPOSITORY NAMING & BOUNDARY VERIFIER")
        print("=" * 80)
        print(f"Platform Authority SHA  : {result.platform_exact_sha}")
        print(f"Policy Version          : {result.policy_version}")
        print(f"Forbidden Symbols Loaded: {result.forbidden_symbols_count}")
        print(f"Repositories Audited    : {', '.join(result.repositories_checked)}")
        print(f"Files Scanned           : {result.total_files_scanned}")
        print(f"Violations Found        : {len(result.violations)}")
        print("-" * 80)

        if result.passed:
            print("  [SUCCESS] All Product service source roots comply with Platform policy.")
            print("=" * 80)
            return 0

        print("  [FAILURE] Policy violations detected:")
        for v in result.violations:
            print(f"  - [{v.repository}] {v.file_path}:{v.line_number}")
            print(f"      Rule   : {v.rule_name} ({v.rule_category})")
            print(f"      Detail : {v.detail}")
            print(f"      Snippet: {v.offending_line}")
        print("=" * 80)
        return 1


if __name__ == "__main__":
    sys.exit(main())
