# Three-repository vertical acceptance

`run.sh` is the reproducible Linux acceptance for the configured connector
vertical slice. It clones exact immutable commits of Cyrene-Platform,
AstrBot-Rev, and Cyrene-Plugins-Official into a private run directory and
does not use the Workspace sibling checkout or IDE project roots.

Before starting the runtime chain, the harness proves the Connector Package
Spec v0.1 lifecycle against the exact Plugins commit. It starts from a clean
Python host where `onebot_v11_connector` is unavailable, builds a deterministic
ZIP and descriptor, verifies both digests, prepares only the exact
`requirements.lock`, installs into a content-addressed host store, and creates
`qq-main` and `qq-secondary` binding records. The same gate also covers cache
corruption rejection, verified offline reinstall, binding-scoped upgrade and
rollback, runtime generation changes, and secret non-persistence.

The runtime portion then starts:

1. two external fake OneBot v11 forward-WebSocket peers;
2. the Platform `configured_binding_server` CES fixture with `qq-main` and
   `qq-secondary` bindings;
3. a disposable PostgreSQL 17 + pgvector container pinned by image digest and
   the real AstrBot database migrator;
4. an AstrBot `WebApplicationFactory<Program>` test host with a deterministic
   test-only worker whose output is persisted by the real PostgreSQL service.

The connector is launched from the verified installed package tree; the source
connector directory is made unavailable before CES starts. The connector,
worker shim, CES Invoke/Subscribe paths, AstrBot connector
subscriber, session/history persistence, and outbound action are real. The
fake peers are only the external OneBot protocol counterpart. Both peers use
the same conversation and message IDs deliberately; the account, binding,
WebSocket endpoint, CES environment, stored identity, and outbound action
must remain isolated.

Run from a Linux checkout:

```bash
./ci/vertical-e2e/run.sh
```

The package lock pins `protobuf==4.25.9`; harness-only test dependencies are
also exact pins. There is no `pip install latest` or source-tree runtime
fallback. Repository refs and URLs can be overridden explicitly:

```bash
CYRENE_PLATFORM_REF=<commit> \
CYRENE_ASTRBOT_REF=<commit> \
CYRENE_PLUGINS_REF=<commit> \
./ci/vertical-e2e/run.sh
```

On success, all child process groups, tracked ports, the owned PostgreSQL
container, installed package store, dependency runtime, registry, and cache are
checked and removed. On failure, the run directory is retained under
`/tmp/cyrene` (or `CYRENE_PHASE7_TMP_PARENT`) for CI artifact upload; process,
port, and container cleanup checks still run.
