# Cyrene Plugin Architecture & Catalog

## 1. Overview
The Cyrene plugin ecosystem allows extending Navigator, Exchange, and daemon capabilities through modular, isolated plugins.

All official plugins are hosted and validated in `DoHorizon-AI/Cyrene-Plugins-Official`.

## 2. Plugin Specification V1 (`plugin.toml`)
Every plugin must define its metadata, capabilities, and dependencies in `plugin.toml` adhering to the V1 schema:

```toml
[plugin]
id = "com.dohorizon.plugin.example"
name = "Example Capability Plugin"
version = "1.0.0"
description = "Demonstrates canonical plugin contract v1"
author = "DoHorizon"
license = "MIT"

[capabilities]
provides = ["media.processor.v1"]
requires = ["model.provider.v1"]

[runtime]
entrypoint = "main:Plugin"
type = "python"
python_version = ">=3.12"
```

## 3. Governance & Quality Gates
1. **No Legacy Surfaces**:
   - `plugin.legacy.toml` and `LEGACY_PLUGIN.md` are strictly prohibited in official plugins.
   - Conformance tests verify the absence of `archive/`, `legacy/`, and legacy gateway imports.
2. **Deferred Exception Policy**:
   - Operator-approved compatibility bridges (e.g. `plugins/**/compatibility/`) are isolated and documented under explicit boundary agreements.
3. **Automated Conformance Testing (TCK)**:
   - Plugin lifecycle tests verify clean load, execute, and unload cycles.
   - Embedding conformance tests ensure cross-plugin vector compatibility.
   - Connector lifecycle checks validate protocol compliance.
