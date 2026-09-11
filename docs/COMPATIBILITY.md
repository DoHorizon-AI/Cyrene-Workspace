# Cyrene Compatibility Architecture & Boundary Standards

## 1. Compatibility Posture
Cyrene maintains a clear distinction between **Permanent Canonical Interfaces** and **Migration/Frozen Compatibility Surfaces**.

### A. Permanent Canonical Interfaces
- **OpenAI Compatible HTTP APIs**:
  - `Cyrene-Reactor` exposes `/v1/chat/completions`, `/v1/completions`, and `/v1/models`.
  - Adheres strictly to standard OpenAI request/response schemas.

### B. Migration & Compatibility Boundaries
Compatibility code remains explicitly scoped and must not become a second canonical runtime:

Under operator decision, `plugins/**/compatibility/` directories in `Cyrene-Plugins-Official` are temporarily retained as approved deferred exceptions while legacy third-party plugins migrate to `plugin.manifest.json`.
