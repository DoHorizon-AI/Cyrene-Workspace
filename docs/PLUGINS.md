# Cyrene Plugin Architecture & Catalog

## 1. Overview
The Cyrene plugin ecosystem allows extending Navigator, Exchange, and daemon capabilities through modular, isolated plugins.

All official plugins are hosted, governed, and conformance-tested in `DoHorizon-AI/Cyrene-Plugins-Official`.

## 2. Canonical Plugin Manifest (`plugin.manifest.json`)
The canonical, authoritative plugin manifest specification for the Cyrene ecosystem is **`plugin.manifest.json`**, governed by the locked JSON schema at `manifests/plugin.manifest.schema.json` in `Cyrene-Plugins-Official`.

Every active plugin must define its identity, entry points, capabilities, methods, and execution modes in its root `plugin.manifest.json`:

```json
{
  "schemaVersion": 1,
  "id": "com.dohorizon.plugin.example",
  "name": "Example Capability Plugin",
  "version": "1.0.0",
  "description": "Canonical Cyrene plugin manifest V1 example",
  "entrypoint": "main.py",
  "runtime": "python",
  "methods": [
    {
      "name": "process",
      "executionMode": "worker"
    }
  ],
  "capabilities": {
    "provides": ["example.processing.v1"],
    "requires": ["model.provider.v1"]
  },
  "distribution": {
    "specVersion": "0.1",
    "publicationStatus": "STABLE"
  }
}
```

### Manifest Invariants
1. **Filename Authority**: Only `plugin.manifest.json` is discovered by the conformance harness and runtime loader. Filenames such as `plugin.toml`, `plugin.legacy.toml`, `manifest.yaml`, or `platform.manifest.json` are forbidden or rejected by the test suite.
2. **Schema Validation**: Every manifest must validate against `manifests/plugin.manifest.schema.json` with `schemaVersion: 1`.
3. **Execution Modes**: Method execution modes (`worker`, etc.) are validated against Platform's canonical `ExecutionMode` resolver.

## 3. Governance & Quality Gates
1. **No Legacy Surfaces**:
   - `plugin.legacy.toml` and `LEGACY_PLUGIN.md` are strictly prohibited in official plugins.
   - Conformance tests (`conformance/tests/test_no_legacy_surface.py`) verify the complete absence of `archive/`, `legacy/`, and legacy gateway imports.
2. **Deferred Exception Policy (Operator Decision D)**:
   - Operator-approved compatibility bridges (under `plugins/**/compatibility/`) are isolated and documented under explicit boundary agreements (`docs/governance/plugin-boundary-inventory.md`).
3. **Automated Conformance Testing (TCK)**:
   - Manifest schema validation across all active plugins.
   - Plugin lifecycle tests verifying clean load, execute, and unload cycles.
   - Embedding conformance tests ensuring cross-plugin vector compatibility.
   - Connector lifecycle checks validating protocol compliance.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 插件架构与目录

## 1. 概览
Cyrene 插件生态通过模块化、相互隔离的插件扩展 Navigator、Exchange 和守护进程的能力。

所有官方插件均托管在 `DoHorizon-AI/Cyrene-Plugins-Official`，并由该仓库治理和执行一致性测试。

## 2. 规范插件清单（`plugin.manifest.json`）
Cyrene 生态的规范权威插件清单是 **`plugin.manifest.json`**；其规范由 `Cyrene-Plugins-Official` 中锁定的 JSON schema `manifests/plugin.manifest.schema.json` 管理。

每个活动插件都必须在根目录的 `plugin.manifest.json` 中声明自身身份、入口点、能力、方法和执行模式：

```json
{
  "schemaVersion": 1,
  "id": "com.dohorizon.plugin.example",
  "name": "Example Capability Plugin",
  "version": "1.0.0",
  "description": "Canonical Cyrene plugin manifest V1 example",
  "entrypoint": "main.py",
  "runtime": "python",
  "methods": [
    {
      "name": "process",
      "executionMode": "worker"
    }
  ],
  "capabilities": {
    "provides": ["example.processing.v1"],
    "requires": ["model.provider.v1"]
  },
  "distribution": {
    "specVersion": "0.1",
    "publicationStatus": "STABLE"
  }
}
```

### 清单不变量
1. **文件名权威性**：一致性 Harness 和运行时加载器只会发现 `plugin.manifest.json`。`plugin.toml`、`plugin.legacy.toml`、`manifest.yaml` 或 `platform.manifest.json` 等文件名均被禁止，或会被测试套件拒绝。
2. **Schema 校验**：每份清单都必须通过 `manifests/plugin.manifest.schema.json` 校验，并使用 `schemaVersion: 1`。
3. **执行模式**：方法的执行模式（例如 `worker`）由 Platform 的规范 `ExecutionMode` 解析器校验。

## 3. 治理与质量门禁
1. **不保留旧版代码面**：
   - 官方插件中严格禁止出现 `plugin.legacy.toml` 和 `LEGACY_PLUGIN.md`。
   - 一致性测试（`conformance/tests/test_no_legacy_surface.py`）会验证不存在 `archive/`、`legacy/` 以及旧版 Gateway 导入。
2. **延后处理的例外策略（运营方决策 D）**：
   - 运营方批准的兼容桥接代码（位于 `plugins/**/compatibility/`）彼此隔离，并依据明确的边界协议（`docs/governance/plugin-boundary-inventory.md`）记录。
3. **自动化一致性测试（TCK）**：
   - 校验所有活动插件的清单 schema。
   - 插件生命周期测试验证干净的加载、执行和卸载流程。
   - Embedding 一致性测试确保插件之间的向量兼容性。
   - Connector 生命周期检查验证协议合规性。
