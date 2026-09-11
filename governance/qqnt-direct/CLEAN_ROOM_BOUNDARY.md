# QQNT Direct Clean-room Boundary

## Decision

Cyrene may design a portless QQ connector profile, but it may not reuse or
depend on NapCat. Native implementation work is gated on confirming a lawful,
documented interface to the official locally installed QQ runtime.

This boundary applies to source, tests, build scripts, generated files,
artifacts, dependency locks, package metadata, and contributor provenance.

## Allowed design inputs

- Cyrene-owned contracts: `message.connector.v1`, Capability Execution Service,
  Capability Binding, worker protocol, application events, Package Spec v0.1,
  PluginManifest, PluginSetLock, and PluginStore.
- Tencent-published QQ installation, version, account, and service
  documentation, subject to its terms.
- A Tencent-authorized native or automation interface and its documentation.
- Public OneBot v11 protocol behavior for the existing compatibility profile.
- Independently authored black-box tests using dedicated accounts and an
  official QQ installation.
- High-level observations in `NAPCAT_ARCHITECTURE_OBSERVATIONS.md`, limited to
  lifecycle requirements and risk identification.

## Prohibited inputs and outputs

- No NapCat code, declarations, tests, schemas, generated files, binaries,
  assets, hooks, packet logic, IPC, or configuration data.
- No copied or translated implementation, including mechanical rewrites in
  another language.
- No reconstruction of exact internal module boundaries, startup logic,
  retries, native loading, event shapes, or media pipelines.
- No NapCat package, source checkout, release, container, or runtime dependency.
- No dynamic download of NapCat during build, install, activation, tests, or
  recovery.
- No use of private QQ protocols, injected hooks, memory modification, or
  reverse engineering unless a specific legal review and written authority
  approves the exact technique.
- No import of existing QQ account/session files except through behavior
  explicitly supported by the official runtime or an authorized interface.

## Contributor provenance protocol

The author of the NapCat observation document is considered research-exposed
and must not author the native wrapper or native loading implementation.

Before implementation begins:

1. nominate an implementation owner who has not inspected NapCat source;
2. provide only these clean-room documents, Cyrene contracts, public protocol
   documents, and approved Tencent interface documentation;
3. record each implementation input by URL, version, license, and approval;
4. require every native-boundary change to include an origin/provenance note;
5. review diffs for suspicious naming, structure, constants, signatures, and
   dependency overlap;
6. retain black-box test recordings and environment manifests without account
   secrets or private user data;
7. reject any contribution whose independent origin cannot be demonstrated.

Automated similarity checks can support review but do not replace provenance or
legal review. NapCat source must not be placed in CI, build caches, test images,
or comparison jobs.

## QQ terms gate

[Tencent's policy index](https://www.tencent.com/zh-cn/policies/) identifies the
[QQ Software License and Service Agreement](https://rule.tencent.com/rule/preview/46a15f24-e42c-4cb6-a308-2347139b1201)
as the governing product agreement. Tencent's published global service terms
also reserve software rights and restrict reverse engineering except where
applicable law or prior consent permits it. These sources do not, by themselves,
authorize the proposed native wrapper. This document records an engineering
gate and is not legal advice.

Required approval record before native implementation:

- exact QQ product/version and operating system;
- exact interface used to load, launch, or automate it;
- whether process injection, patching, private symbols, or binary modification
  occurs;
- distribution model for the Cyrene package and any helper binary;
- account class and automation use case;
- counsel/owner decision and any Tencent permission;
- permitted test environments and rate limits.

If the approved interface requires injection, patching, private protocol
reconstruction, or redistribution of QQ native libraries, the current design
must return to architecture review rather than treating those techniques as an
implementation detail.

## Technical boundary

The default runtime chain is:

```text
Product
  -> CapabilityExecutionService
  -> binding-scoped canonical worker over stdio
  -> Cyrene QQ connector worker
  -> optional Cyrene native helper over inherited stdio
  -> official, locally installed QQ runtime
```

No stage opens an HTTP, WebSocket, or other TCP listening port. If the native
helper cannot inherit stdio, the only allowed fallback is an owner-only Unix
domain socket on POSIX or an owner-restricted named pipe on Windows. The helper
must not be reachable beyond the binding's process tree.

The native helper is a narrow authorized adapter. It does not own Product
policy, OneBot compatibility, session history, memory, moderation, routing, or
application persistence.

## Package boundary

`qqnt-direct` ships in the same immutable installable connector package lineage
as the existing official OneBot v11 connector. It is a runtime profile, not a
second catalog or a second capability.

The package may share immutable code and digest-addressed dependency runtime
across bindings. Binding configuration, secret references, account state,
native session, worker process, event stream, and outbound route are never
shared.

The package must contain no official QQ binary or library unless Tencent has
explicitly authorized redistribution. Normal operation discovers a separately
installed official QQ runtime and verifies compatibility before activation.

## Clean-room acceptance gates

- dependency graph contains no NapCat package, repository URL, binary, or
  derived artifact;
- source and generated-output review finds no prohibited upstream material;
- package build succeeds without a NapCat checkout or network access to NapCat;
- runtime starts only from the installed package and an official local QQ
  installation;
- legal/terms approval identifies the exact native interface;
- unsupported QQ builds fail closed before account state is touched;
- two bindings have separate process trees, data directories, secret handles,
  sessions, and event routes;
- shutdown leaves no child process, IPC endpoint, lock, or temporary secret;
- port scan proves zero connector-owned listening TCP ports.

## Current boundary status

- NapCat code reuse: **prohibited and absent by design**.
- NapCat runtime dependency: **prohibited and absent by design**.
- Portless process architecture: **frozen**.
- Native implementation authorization: **not yet established**.
