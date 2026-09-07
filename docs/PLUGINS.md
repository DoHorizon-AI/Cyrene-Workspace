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
