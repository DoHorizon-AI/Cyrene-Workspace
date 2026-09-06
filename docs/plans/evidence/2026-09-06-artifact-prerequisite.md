# Portable directory Artifact prerequisite / 可移植目录产物前置证据

Recorded: 2026-09-06. Scope: the shared directory contract, Python provider and
Rust materializer for Linux execution nodes.
This is a prerequisite for model import and transfer; it does not accept a
Reactor Deployment, a GPU execution, or any Phase 1/4 gate.

记录日期：2026-09-06。本记录覆盖共享目录契约、Python provider 与 Linux Rust 落盘。
它为模型导入和
传输提供前置能力，不代表 Reactor Deployment、GPU 执行或 Phase 1/4 总门通过。

## Sources and delivery / 来源与交付

- Platform commit: `1b0bb98253b9ca48cb56b6b3b73f2f53f6c81065`.
- Rust materializer commit: `30272145b9c11df9948465359479c3d95d08a1dc`.
- Branch: `feat/lifecycle-artifact-bundles-v1`.
- [Draft PR 31](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/31) is stacked
  on `feat/text-model-lifecycle-v1` at
  `185527f82c83700d4f567ac563a393a2e1c18c87` / PR 30.
- Exact-commit hosted checks passed:
  [CI 34020951123](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/34020951123)
  and [cross-repo 34020951148](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/34020951148).
  The main CI includes documentation, governance, JVM, Python SDK and Rust jobs.
- Both checks also passed for the materializer revision:
  [CI 34022189701](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/34022189701)
  and [cross-repo 34022189566](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/34022189566).

以上提交已推送且对应 CI 通过，仍未合并至 main/develop；Draft PR 的堆叠关系不是
canonical 接受证明。两个 SHA 的能力和验证分别记录。

## Contract and provider / 契约与 provider

`PortableDirectoryManifest` V2 identifies a directory by the JCS canonical
object `{files, size_bytes, version}`. Each file has a relative path, raw-byte
SHA-256 and size. `ArtifactRef.digest` and `manifest_digest` identify the
canonical manifest; `ArtifactRef.size_bytes` is the logical sum of member
bytes. Product metadata and source locators remain outside that identity.
The existing V1 local-directory representation remains compatible.

Rust 与 Python 使用同一份 V2 契约。路径校验拒绝绝对路径、父目录跳转、重复路径、
文件/目录前缀冲突和 ASCII 大小写别名；拒绝非法 digest 及超过 JCS 精确整数范围的
大小。Unicode 路径和空文件保留。契约当前以 Linux 为执行目标，不宣称已处理
Windows 保留文件名、ADS 或所有文件系统的 Unicode 归一化差异。

Python `LocalArtifactProvider.publish_portable_directory()` validates the
input tree, streams members into CAS, and publishes its canonical index last.
Resolve verifies the stored canonical bytes, root identity, logical size and
each member. A failed publish may leave unreferenced CAS blobs; it does not
publish a partial manifest. Source-tree traversal errors are raised.

Python provider 的身份验证与 CAS 原始字节校验均有覆盖。`cy-manifest --type
portable-directory` 是内容身份计算入口，可接受等价的普通 JSON/YAML 表示；它不把
非 canonical 的输入表示误判成另一种目录身份。

## Verification / 验证

| Check | Command or evidence | Result |
| --- | --- | --- |
| Rust contract | `cargo test --locked -p cy-manifest` | 24 passed, 0 failed |
| Python provider | `PYTHONPATH=sdk/python/cyrene_artifacts/src <pytest-python> -m pytest sdk/python/cyrene_artifacts/tests -q` | 13 passed, 0 failed |
| Rust transfer/materializer | `cargo test --locked -p cy-artifact-transfer` at `3027214` | 15 unit + 8 integration passed, 0 failed, 0 ignored |
| Python typing | `mypy sdk/python/cyrene_artifacts/src` | 3 files, no issues |
| Repository documentation | `python tooling/docs/validate_docs.py` | 100% |
| Scoped quality | Rust format/clippy, Python Ruff and `git diff --check` | Passed |
| Cross-language fixture | `contracts/schemas/examples/portable_directory_manifest.example.json` | Rust and Python agree on identity |

The Unicode/empty-file fixture contains `weights.bin` = `abc`, `z/é.txt` =
bytes `00 ff`, and empty `模型/空.txt`: 3 files, 5 logical bytes. Its manifest
identity is
`sha256:5d3c4bb7f4b864c409fa3baafb199f33f54c5fdfead4c6ec724f16898b2a828f`.

本地验证环境为 Linux/WSL，Platform 使用 Rust 1.96.1。Python 测试使用已有的
Navigator 开发虚拟环境；另一个临时 Python 3.12.11 环境运行 mypy 1.19.1。
全局 Python 缺少 pytest 的初次环境错误未被算作测试通过。

## Linux materialization / Linux 目录落盘

The Rust API consumes an authorized ArtifactRef, manifest bytes and a
caller-owned digest byte source. It streams each member with a size bound,
verifies SHA-256, synchronizes the complete directory tree, and publishes with
Linux `renameat2(RENAME_NOREPLACE)`. An existing target is reused only after a
complete tree/content verification. A deterministic syscall regression confirms
that a competing empty directory keeps its inode and contents.

跨 SDK 集成测试实际启动 Python subprocess，调用 `publish_portable_directory()`，
通过测试适配器导出中性的 manifest/ArtifactRef/digest blobs，再由 Rust 落盘并逐文件
比较。生产 Rust API 不知道 Python provider 的私有 CAS 路径。测试还覆盖 Unicode、
空文件、普通 pretty JSON、坏/缺 blob、超长流、读取故障、目标冲突及静态 symlink。

This V1 consumer is Linux-only. Its immediate parent must already exist in a
trusted owner-controlled staging root; the caller owns ancestor durability and
external-writer coordination. It does not claim arbitrary same-UID race
protection. A post-rename parent fsync error can leave a complete visible target,
which a retry verifies; failed staging and crash residue require caller cleanup.

当前不宣称 Windows 目录落盘已通过。普通校验或读取失败不会发布半成品；最后一步
目录项同步失败可能发生在完整目录已可见之后，因此返回错误不等于“目标必定不存在”。

## Remaining integration / 尚未完成的集成

RuntimeAgent consumption, verified model acquisition, vLLM lifecycle and
cross-node transfer remain separate work.
Reactor must still prove actual model loading and inference through Platform.
The manually launched vLLM used by Phase 0 is an external Provider fixture.

RuntimeAgent 接入、模型获取、vLLM 生命周期和跨节点传输仍需独立
验收。P1-02、P1-04、P4-07 和所有阶段总门保持未勾选；本前置切片不绕过 Navigator
adoption decision。
