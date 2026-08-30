# Three-repository vertical acceptance

`run.sh` is the reproducible Linux acceptance for the configured connector
vertical slice. It clones exact immutable commits of Cyrene-Platform,
AstrBot-Rev, and Cyrene-Plugins-Official into a private run directory and
does not use the Workspace sibling checkout or IDE project roots.

The default run starts:

1. two external fake OneBot v11 forward-WebSocket peers;
2. the Platform `configured_binding_server` CES fixture with `qq-main` and
   `qq-secondary` bindings;
3. a disposable PostgreSQL 17 + pgvector container pinned by image digest and
   the real AstrBot database migrator;
4. an AstrBot `WebApplicationFactory<Program>` test host with a deterministic
   test-only worker whose output is persisted by the real PostgreSQL service.

The connector, worker shim, CES Invoke/Subscribe paths, AstrBot connector
subscriber, session/history persistence, and outbound action are real. The
fake peers are only the external OneBot protocol counterpart. Both peers use
the same conversation and message IDs deliberately; the account, binding,
WebSocket endpoint, CES environment, stored identity, and outbound action
must remain isolated.

Run from a Linux checkout:

```bash
./ci/vertical-e2e/run.sh
```

The harness uses a pinned `protobuf==4.25.8` test dependency. It never runs
`pip install latest`. Repository refs and URLs can be overridden explicitly:

```bash
CYRENE_PLATFORM_REF=<commit> \
CYRENE_ASTRBOT_REF=<commit> \
CYRENE_PLUGINS_REF=<commit> \
./ci/vertical-e2e/run.sh
```

On success, all child process groups, tracked ports, and the owned PostgreSQL
container are checked and removed. On failure, the run directory is retained
under `/tmp/cyrene` (or `CYRENE_PHASE7_TMP_PARENT`) for CI artifact upload;
the cleanup checks still run.
