# Cyrene Compatibility Architecture & Boundary Standards

## 1. Compatibility Posture
Cyrene maintains a clear distinction between **Permanent Canonical Interfaces** and **Migration/Frozen Compatibility Surfaces**.

### A. Permanent Canonical Interfaces
- **OpenAI Compatible HTTP APIs**:
  - `Cyrene-Reactor` exposes `/v1/chat/completions`, `/v1/completions`, and `/v1/models`.
  - Adheres strictly to standard OpenAI request/response schemas.
- **AstrBot DotNet Host HTTP Surface**:
  - Exposes `/api/v1/*` endpoints for bot management and message routing.

### B. Migration & Compatibility Boundaries (AstrBot)
The AstrBot architecture underwent systematic component classification to eliminate ambiguous legacy states:

| Component | Target Artifact | Architectural Classification | Lifecycle Posture |
| --- | --- | --- | --- |
| 1. `COMPATIBILITY_BRIDGE_AUTH` | `CompatibilityBridgeAuthHeader.cs` | `LIVE_INTERNAL_PRIMITIVE` | Migration Only |
| 2. `COMPATIBILITY_PROXY_SERVICE` | `CompatibilityProxyService.cs` | `DEVELOPMENT_ONLY` | Throws in Production |
| 3. `COMPATIBILITY_FALLBACK_COMPAT_ROUTE` | `/compat/{**path}` in `CompatibilityFallbackEndpoints.cs` | `DEVELOPMENT_ONLY` | Throws in Production |
| 4. `PYTHON_COMPAT_CONTAINER` | `python-compat` in `compose.python-compat.yml` | `DEVELOPMENT_ONLY` | Dev test environment |
| 5. `COMPOSE_PYTHON_COMPAT` | `deploy/compose/compose.python-compat.yml` | `DEVELOPMENT_ONLY` | Dev test orchestration |
| 6. `OLD_PYTHON_PLUGIN_RUNTIME` | `plugin_compat.py` | `FROZEN_COMPATIBILITY` | Migration Only |
| 7. `PYTHON_CAPABILITY_WORKER` | `python/capability_worker/src` | `LIVE_SUPPORTED` | Canonical Cyrene worker |
| 8. `OPENAI_COMPATIBLE_PROVIDER_ADAPTER` | `OpenAiCompatibleProviderAdapter.cs` | `LIVE_SUPPORTED` | Active production DI singleton |
| 9. `SPA_STATIC_FILE_FALLBACK` | `MapFallback` to `wwwroot/index.html` | `LIVE_SUPPORTED` | Active production web UI |

### C. Deferred Plugin Exceptions
Under operator decision, `plugins/**/compatibility/` directories in `Cyrene-Plugins-Official` are temporarily retained as approved deferred exceptions while legacy third-party plugins migrate to `plugin.toml` V1.
