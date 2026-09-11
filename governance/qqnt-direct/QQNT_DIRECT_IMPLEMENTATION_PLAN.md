# QQNT Direct Implementation Plan

## Scope

This plan defines the first independently implementable slice after legal and
native-interface approval. It does not authorize implementation and does not
include code from the research reference.

## Entry gates

Implementation starts only when all are true:

1. QQ product terms and the exact native/automation technique have written
   approval from the responsible owner or counsel.
2. A Tencent-published or explicitly authorized interface is identified. The
   plan must return to architecture review if the technique requires injection,
   patching, private protocol reconstruction, or QQ binary redistribution.
3. The implementation owner has not inspected NapCat source and accepts the
   provenance protocol in `CLEAN_ROOM_BOUNDARY.md`.
4. Exact supported QQ version/build, operating system, CPU architecture, and
   installation source are pinned.
5. Dedicated test accounts and an isolated test environment are available.
6. The existing package lifecycle and P2.5 vertical gate pass at current
   canonical repository SHAs.

## Minimal slice

### Slice 1: package/profile projection

- version the existing official connector package rather than creating a
  second package registry;
- add `qq` family, `qqnt-direct` runtime profile, and `onebot-v11`
  compatibility-profile metadata through current PluginManifest and Package
  Spec authorities;
- keep the existing OneBot transport profile functional;
- update the configuration schema so direct and OneBot fields are mutually
  exclusive;
- produce one immutable artifact, exact dependency lock, digest, provenance,
  and SBOM hook;
- prove no QQ or NapCat binaries are bundled without explicit distribution
  authority.

### Slice 2: official QQ installation discovery

- implement independent platform adapters for supported installation layouts
  using public OS/package metadata or operator-provided paths;
- canonicalize executable and data paths;
- detect zero, one, or multiple installations deterministically;
- collect QQ version/build, OS, and architecture without modifying the
  installation;
- reject unknown or unapproved builds before loading the native boundary.

Initial support should select one host/QQ combination only. Additional layouts
are separate compatibility work, not heuristic fallback.

### Slice 3: Cyrene native wrapper boundary

- define the Cyrene-owned versioned stdio helper protocol;
- implement the smallest authorized adapter for runtime initialization, login,
  session readiness, text receive, text send, and shutdown;
- place the helper beside the official runtime on the native OS;
- use inherited stdio by default; use owner-only UDS/named pipe only if proven
  necessary;
- forbid TCP listeners and enforce this in tests;
- keep native implementation in a separately reviewed package area with a
  provenance manifest.

### Slice 4: login and session

- support reuse of an officially recognized existing session in the binding's
  private data directory;
- otherwise expose QR login progress through a bounded administrative event;
- validate the resulting QQ account against binding configuration;
- start one native session for one binding generation;
- do not parse or migrate private QQ/NapCat session formats;
- do not add password, captcha bypass, device spoofing, or risk-control
  circumvention to the minimal slice.

### Slice 5: direct message mapping

- normalize private text receive directly to `message.connector.v1`;
- normalize group text receive directly to `message.connector.v1`;
- implement `send_message` for private and group text;
- preserve binding, account, conversation, sender, message, and generation
  identity separately;
- implement deadlines, cancellation, correlation, and generic CES errors;
- do not round-trip internal data through OneBot JSON.

### Slice 6: supervision and isolation

- activate one worker/helper/native session process tree per binding;
- create binding-private account data, temporary, log, and IPC roots;
- share only immutable package and locked dependency runtime by digest;
- implement bounded restart/backoff and a binding-local crash circuit;
- preserve binding ID and change generation on restart;
- drain and reap every process and IPC resource on shutdown.

## Test strategy

### Hermetic tests

- package metadata and profile selection conformance;
- configuration mutual exclusion and secret-reference handling;
- installation discovery over synthetic filesystem/package-manager fixtures;
- helper protocol framing, negotiation, correlation, cancellation, and malformed
  input using an independently authored fake native counterpart;
- direct canonical mapping for private/group text;
- no OneBot serialization in `qqnt-direct` execution;
- zero listening ports;
- process-tree restart, timeout, cancellation, and shutdown cleanup;
- static dependency/provenance scan proving zero NapCat material.

### Binding isolation test

Run `qq-main` and `qq-secondary` concurrently with overlapping native message,
conversation, and trace-like identifiers:

- A receive reaches only A subscription;
- B receive reaches only B subscription;
- Invoke A reaches only A native counterpart;
- Invoke B reaches only B native counterpart;
- account data, secrets, correlation, queues, and logs remain separate;
- restart A changes only A generation;
- immutable package code and dependency runtime are reused by digest.

### Authorized official-runtime smoke

On the approved host and QQ build:

- discover the official installation;
- existing-session or QR login with a dedicated test account;
- receive private text;
- receive group text;
- send private text;
- send group text;
- reconnect after controlled process interruption;
- verify binding/account identity after restart;
- shut down with no orphan process, IPC endpoint, lock, or listening port.

This smoke is not replaceable by a NapCat deployment. NapCat is neither the
runtime under test nor a fixture.

## P0/P2.5 harness extension

Extend the committed three-repository vertical harness instead of creating a
parallel framework:

1. build and install the new version of the same official connector package;
2. activate two `qqnt-direct` bindings from the installed artifact;
3. provide an independently authored fake native helper counterpart for CI;
4. run real CES Invoke/Subscribe, AstrBot host, and disposable pgvector;
5. assert database/state/outbound isolation for overlapping identifiers;
6. repeat offline reinstall, corruption rejection, upgrade, rollback, and
   cleanup checks;
7. assert the source checkout is unavailable at runtime;
8. assert no OneBot endpoint and no TCP listener exist in direct mode.

The authorized official-runtime smoke remains a separate protected/nightly gate
because it requires QQ installation, terms-approved execution, and test-account
credentials.

## Repository ownership

- Cyrene-Platform: no QQ semantics; only existing generic CES/binding/worker
  behavior unless a generic defect is proven.
- Cyrene-Plugins-Official: package profiles, connector worker, native adapter,
  protocol mapping, package tests, and provenance.
- AstrBot-Rev: Product binding configuration and Product/session policy only;
  no native QQ fallback.
- Cyrene-Workspace: package/profile governance, clean-room evidence, accepted
  baselines, and repeatable vertical CI orchestration.

Each repository change uses its own task branch and canonical workflow.

## Stop conditions

- no written authority for the exact QQ native technique;
- implementation contributor has unbounded NapCat source exposure;
- required behavior can be achieved only through prohibited copying, injection,
  patching, private-protocol reconstruction, or unauthorized redistribution;
- unsupported QQ build is being accepted through heuristic fallback;
- direct mode opens a TCP listener or falls back to OneBot;
- one binding can observe another binding's account, events, secrets, state, or
  outbound route;
- package lifecycle needs `pip install latest` or a source-tree fallback.

## Definition of done for the first slice

- all entry gates recorded and approved;
- immutable package installs through Package Spec v0.1 lifecycle;
- clean host has no preinstalled connector or NapCat material;
- authorized official QQ runtime is discovered and version-gated;
- existing-session or QR login succeeds for one binding;
- private/group text receive and send pass;
- two-binding isolation passes;
- restart preserves binding and changes generation;
- stdio is the default IPC and connector-owned TCP listeners remain zero;
- P2.5-derived CI gate and protected official-runtime smoke pass;
- cleanup proves no orphan process, IPC endpoint, port, container, or temporary
  package/account state;
- clean-room provenance review is signed off.
