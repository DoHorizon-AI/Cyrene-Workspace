# QQNT Direct Runtime Specification

## Status

Design-only specification. No native runtime implementation is authorized by
this document. Entry into implementation requires the gates in
`CLEAN_ROOM_BOUNDARY.md`.

## Identity model

| Identity | Meaning | Stable across restart | Sharing rule |
| --- | --- | --- | --- |
| Package artifact digest | Immutable connector code and locked dependencies | Yes | May be shared by many bindings. |
| Capability ID | What the connector can do: `message.connector.v1` | Yes | Shared by every compatible binding. |
| Binding ID | Which configured connector instance handles a request | Yes | Never shared as runtime state. |
| QQ account ID | Account selected after successful login | Yes, subject to operator configuration | Exactly one account per v0.1 binding. |
| Worker generation | One execution attempt of a binding | No | Changes after restart and is never an address. |
| Native session ID | One live official QQ session inside one generation | No | Owned by exactly one binding generation. |

The binding ID is the CES target. The QQ account ID is validated against the
binding's configured expectation after login but does not replace the binding
ID. A PID, process handle, native session, or worker generation must never be
persisted as configured identity.

## Package and profile model

- capability: `message.connector.v1`
- connector family: `qq`
- runtime profile: `qqnt-direct`
- compatibility profile: `onebot-v11`
- package: the same immutable official connector package lineage that currently
  carries the OneBot v11 profile

`qqnt-direct` maps authorized native QQ observations directly to the canonical
message contract. It does not serialize internal events as OneBot JSON and
parse them again. The `onebot-v11` profile remains a separately selected
compatibility transport within the package.

Changing the existing package ID is outside this design task. The implementation
change must version the current package and project the family/profile metadata
through the existing PluginManifest and Package Spec authority, without a new
registry or duplicate descriptor.

## Process architecture

```text
AstrBot or another Product
        |
        | Invoke / Subscribe(binding_id)
        v
Platform CapabilityExecutionService
        |
        | canonical worker protocol, inherited stdin/stdout
        v
QQ connector worker (one binding generation)
        |
        | inherited stdio
        | or owner-only UDS / named pipe if native isolation requires it
        v
Cyrene native helper (optional, one binding generation)
        |
        | authorized local integration boundary
        v
Official locally installed QQ runtime (one account/session)
```

The connector and helper open zero TCP listening ports. Outbound connections
made by the official QQ runtime remain owned by QQ and are not connector IPC.

## Host placement

The native helper and official QQ process must run on the same operating-system
side as the installed QQ runtime.

- Native Linux host: run worker/helper/QQ on Linux; a display/session provider
  may be required by the official package.
- Windows host: run native helper/QQ on Windows.
- Canonical desktop WSL host: Platform may remain in WSL. A Windows helper may
  be launched through WSL interoperability with inherited stdio and explicit
  process ownership. If reliable inherited stdio and process reaping cannot be
  proven, use an owner-restricted Windows named pipe bridged by a small
  Cyrene-owned launcher. Do not substitute localhost TCP.

Cross-boundary paths are configuration inputs resolved to canonical real paths.
Secrets and account data must stay on the native-runtime side and must not be
copied into the WSL package cache.

## Configuration model

The eventual schema must be versioned and binding-scoped. It should contain
only values or secret references needed for:

- `binding_id`;
- `runtime_profile = qqnt-direct`;
- expected QQ account identifier, optional before first QR login;
- official QQ installation discovery mode and optional explicit executable;
- binding-owned data directory;
- login policy: `existing_session` or `qr`;
- compatibility allow-list for QQ version/build and platform;
- restart budget and operation timeout bounds;
- secret references, never inline persistent secret values.

The OneBot endpoint/token fields are invalid when `runtime_profile` is
`qqnt-direct`. Explicit direct binding selection must never fall back to a
global OneBot endpoint.

## Runtime state machine

```text
CREATED
  -> DISCOVERING
  -> COMPATIBILITY_VERIFIED
  -> NATIVE_READY
  -> LOGIN_REQUIRED | LOGIN_IN_PROGRESS
  -> SESSION_STARTING
  -> READY
  -> DRAINING
  -> STOPPED
```

Any state may enter `FAILED`. A restart creates a new worker generation and a
new native session while preserving the binding ID. `READY` is reached only
after account identity is known, matches binding policy, the native session is
ready, and event publication is attached.

Unsupported QQ version, ambiguous installation discovery, account mismatch,
login rejection, helper protocol mismatch, and unavailable native runtime fail
closed. No request is queued for an arbitrary installation, account, or
binding.

## Native helper protocol

The helper protocol is a new Cyrene-owned, versioned, length-delimited protocol
over stdio. It carries:

- startup negotiation and supported operation versions;
- binding and generation correlation established by the parent;
- compatibility report for the discovered official runtime;
- login status and QR presentation payload or secure reference;
- session-ready/account-confirmed status;
- normalized private/group text receive events;
- text-send command and correlated result;
- cancellation, drain, and shutdown control;
- bounded diagnostic events without secrets or message contents by default.

The helper protocol does not expose CES directly and is never a Product API.
It contains no OneBot envelope. Correlation identifiers are unique within one
worker generation and cannot route across bindings.

## First-slice message mapping

### Receive

- private text -> canonical inbound message with private conversation scope;
- group text -> canonical inbound message with group conversation scope;
- content order preserved for the single supported text part;
- connector family, binding ID, self account, sender, conversation, message,
  timestamp, and worker generation are stamped from the active binding/session;
- duplicate suppression, if required, is keyed by binding plus native message
  identity, never by message ID alone.

### Send

- CES resolves an explicit or unambiguous binding before worker activation;
- the worker validates the request binding against its immutable startup
  binding;
- destination scope and text payload are validated;
- the command is sent only to that binding's helper/session;
- cancellation or deadline aborts correlation and returns the existing generic
  CES error model;
- result includes the binding-stamped external message reference when available.

Rich media, replies, mentions, proactive-send policy, and Product session policy
are outside the minimal implementation slice.

## Binding isolation requirements

For `qq-main` and `qq-secondary`:

- one worker process tree per active binding generation;
- one helper and one official QQ native session per binding;
- separate data directory, lock file, session state, secret handles, logs, and
  temporary media area;
- no shared mutable singleton, event bus, correlation table, login state, or
  outbound queue;
- immutable package/runtime files may be shared read-only by artifact digest;
- configuration is materialized separately for each activation;
- every inbound event is binding-stamped before it enters a shared Platform
  component;
- every outbound request is checked against the worker's startup binding;
- restart of one binding does not restart, drain, or mutate the other.

## Restart and shutdown

- unexpected process exit marks only that binding generation unavailable;
- restart uses a host-owned bounded backoff and creates a new generation;
- existing account state may be reused only through officially supported
  session behavior and only from that binding's data directory;
- repeated startup failures open a binding-local circuit and require operator
  action or an explicit retry window;
- graceful shutdown stops new invokes, cancels subscriptions, drains bounded
  in-flight sends, requests native session shutdown, closes IPC, and reaps the
  whole process tree;
- forced termination is bounded and followed by checks for child processes,
  IPC endpoints, file locks, and connector-owned listening ports.

## Security and observability

- secret values are resolved at activation and never written to package
  descriptors, logs, crash reports, or the package cache;
- QR data is short-lived, access-controlled, and redacted from normal logs;
- native helper and worker authenticate their inherited channel through process
  ownership plus a one-generation nonce;
- owner-only permissions are mandatory for UDS/named-pipe fallback;
- diagnostics identify package digest, binding ID, QQ build, state, and worker
  generation without exposing account credentials or message bodies;
- CI asserts zero connector-owned listening TCP sockets throughout startup,
  login simulation, send/receive, restart, and shutdown.

## Deterministic errors

- official QQ installation not found;
- multiple installations without an explicit selection;
- unsupported QQ version/build or native ABI;
- native interface not authorized/available;
- login required, rejected, expired, or account mismatch;
- binding unavailable or restarting;
- helper protocol incompatibility;
- send timeout or cancellation;
- native session closed;
- clean shutdown incomplete.

These map into existing generic Platform/CES status categories. No NapCat- or
Product-specific error is added to CES.
