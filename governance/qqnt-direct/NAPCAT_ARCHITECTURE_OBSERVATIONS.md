# NapCat Architecture Observations

## Purpose and research boundary

This document records only high-level architecture and externally observable
behavior needed to define an independent Cyrene QQ runtime. It is not an
implementation guide. No NapCat source, type declaration, test, native hook,
schema, generated file, algorithm, or line-by-line structure may be reused.

The read-only reference was pinned before inspection:

- repository: `NapNeko/NapCatQQ`
- commit: `3ac54c181b5e74d7acee5a62293ade88630b05ba`
- remote authority checked: repository `HEAD` and `main` resolved to that commit
- license at the pinned commit: restrictive custom Limited Redistribution
  License

The reference was inspected in a detached, temporary read-only clone. It is not
a Cyrene dependency, submodule, fork, or distribution input.

## Classification vocabulary

Every research finding below has exactly one classification:

- `OBSERVED_ARCHITECTURE`: high-level component or process organization visible
  in the pinned repository or its official documentation.
- `OBSERVED_RUNTIME_BEHAVIOR`: externally meaningful lifecycle behavior visible
  in the pinned repository or its official documentation.
- `PUBLIC_PROTOCOL_FACT`: behavior defined by public OneBot or Cyrene contracts,
  independent of NapCat's implementation.
- `INFERENCE`: a design-relevant conclusion that is not an asserted upstream
  contract.
- `UNKNOWN`: not established by the permitted research.
- `FORBIDDEN_TO_REUSE`: implementation material that may have been visible but
  must not enter Cyrene source, tests, schemas, or generated artifacts.

## Sources

- [Pinned NapCat repository](https://github.com/NapNeko/NapCatQQ/tree/3ac54c181b5e74d7acee5a62293ade88630b05ba)
- [Pinned NapCat license](https://github.com/NapNeko/NapCatQQ/blob/3ac54c181b5e74d7acee5a62293ade88630b05ba/LICENSE)
- [NapCat Shell documentation](https://napneko.github.io/guide/boot/Shell)
- [NapCat Framework documentation](https://napneko.github.io/guide/boot/Framework)
- [NapCat installation documentation](https://napneko.github.io/guide/install)
- [NapCat basic configuration documentation](https://napneko.github.io/config/basic)
- [NapCat advanced configuration documentation](https://napneko.github.io/config/advanced)
- [NapCat security documentation](https://napneko.github.io/other/security)
- [Tencent policy index](https://www.tencent.com/zh-cn/policies/)
- [Official QQ for Linux download page](https://im.qq.com/linuxqq/index.shtml)

## Audit findings

| ID | Classification | Area | High-level finding | Clean-room consequence |
| --- | --- | --- | --- | --- |
| NC-01 | `OBSERVED_ARCHITECTURE` | Monorepo structure | The pinned project is a private-package TypeScript monorepo divided into launch/supervision, native QQ access, core messaging, protocol adapters, media, persistence, Web UI, and build-support areas. | Cyrene may use its own existing Platform/Plugin boundaries; it must not reproduce the upstream package graph or names. |
| NC-02 | `OBSERVED_ARCHITECTURE` | Native loading boundary | A platform-specific boundary discovers an installed QQ runtime, determines its version/build metadata, and establishes access to QQ facilities before login/session startup. | Discovery, compatibility checking, and native access must be independent Cyrene work with documented legal authority. |
| NC-03 | `OBSERVED_RUNTIME_BEHAVIOR` | Initialization | Runtime startup is ordered: environment and version discovery precede native access; engine/login readiness precedes account login; account login precedes the account session and message listeners. | The Cyrene worker needs an explicit state machine and must reject operations before readiness. |
| NC-04 | `OBSERVED_RUNTIME_BEHAVIOR` | Login | Official project behavior includes QR login and reuse of an eligible existing account session, with asynchronous progress and failure notifications. | The first Cyrene slice may expose QR and existing-session login states, but must derive its implementation from an authorized QQ boundary and black-box evidence only. |
| NC-05 | `OBSERVED_ARCHITECTURE` | Event model | Native service notifications are normalized by a central core event layer before protocol adapters publish them. | Cyrene should normalize authorized native observations directly into `message.connector.v1`; it need not introduce an internal OneBot representation. |
| NC-06 | `OBSERVED_ARCHITECTURE` | Receive boundary | Incoming QQ messages enter through native service listeners and are then routed to higher-level adapters. | The Cyrene-owned native adapter is the receive authority for one binding and must stamp that binding before publication. |
| NC-07 | `OBSERVED_ARCHITECTURE` | Send boundary | Outbound protocol actions are translated into calls to QQ message facilities behind the native boundary. | `send_message` must resolve one binding, then use only that binding's native session. |
| NC-08 | `OBSERVED_ARCHITECTURE` | Rich media | Rich media crosses separate upload/download and media-processing boundaries and may require external media tools or platform-specific helpers. | Text-only is the first slice. Image/file support requires a later, independently specified media lifecycle and dependency review. |
| NC-09 | `OBSERVED_RUNTIME_BEHAVIOR` | Version discovery | Compatibility depends on installed QQ version/build metadata, operating-system layout, CPU architecture, and native component compatibility. | Discovery must return a compatibility report before activation; an unknown build fails closed. |
| NC-10 | `OBSERVED_ARCHITECTURE` | Supervision | Shell operation separates a supervisor from a worker/native runtime process. | Cyrene may independently use its existing worker supervision model; no upstream process protocol or retry algorithm may be reused. |
| NC-11 | `OBSERVED_RUNTIME_BEHAVIOR` | Restart lifecycle | Unexpected worker exits are observed, restart attempts are bounded, crash loops are distinguished from healthy operation, and shutdown escalates when graceful termination does not complete. | Define Cyrene-owned restart budgets, generation changes, and bounded shutdown in the runtime specification. |
| NC-12 | `OBSERVED_RUNTIME_BEHAVIOR` | Shutdown | Interrupt/termination signals enter an explicit worker shutdown path before forced termination. | CES cancellation and host shutdown must stop subscriptions, close the native session, and reap every owned process. |
| NC-13 | `OBSERVED_ARCHITECTURE` | Multi-account | Official Shell documentation advertises account selection and an external desktop manager that can manage multiple accounts. | Multi-account capability is observable, but it does not establish that one native process safely owns multiple sessions. |
| NC-14 | `INFERENCE` | Multi-account | The inspected lifecycle is consistent with one selected account and one native session per worker process. | Cyrene v0.1 adopts one binding per native session/process as the conservative isolation boundary. This is a Cyrene decision, not an upstream guarantee. |
| NC-15 | `OBSERVED_ARCHITECTURE` | Platform dependencies | Official deployment modes vary across Windows, Linux, containers, and AppImage; Linux documentation references display/session dependencies, while Windows includes platform-specific launch support. | Native process placement and GUI/session availability must be explicit installation compatibility dimensions. |
| NC-16 | `OBSERVED_RUNTIME_BEHAVIOR` | Network adapters | NapCat's public integration path commonly exposes OneBot over HTTP or WebSocket, while its Web UI can be separately configured. | This behavior is not adopted by `qqnt-direct`; the default Cyrene profile has no TCP listener and no localhost protocol hop. |
| NC-17 | `PUBLIC_PROTOCOL_FACT` | OneBot compatibility | OneBot v11 defines public action/event semantics that can be tested independently of any one implementation. | The existing `onebot-v11` profile remains a compatibility profile in the same package; it is not the internal data model for `qqnt-direct`. |
| NC-18 | `OBSERVED_RUNTIME_BEHAVIOR` | Account safety | Official NapCat security guidance warns about account restrictions, disconnects, and environment/account separation. | Live testing requires dedicated test accounts, bounded rates, explicit operator consent, and a kill switch. |
| NC-19 | `UNKNOWN` | Supported native API | The research did not establish a Tencent-published, stable native automation API for desktop QQ. | Implementation must not begin native integration until an authorized interface or written permission is established. |
| NC-20 | `UNKNOWN` | Terms authorization | The research did not establish that QQ's standard desktop license authorizes third-party native wrapping, automation, injection, or reverse engineering. | Legal/terms review is a blocking implementation gate, not a documentation caveat. |
| NC-21 | `UNKNOWN` | Session format | No reusable specification for QQ's local account/session storage was established. | Existing-session login may use only official behavior or an authorized API; Cyrene must not parse or mutate private session formats. |
| NC-22 | `UNKNOWN` | Stable event schema | No Tencent-published stable schema for QQ desktop's internal events was established. | The native adapter contract must be Cyrene-owned and version-gated; unsupported builds fail closed. |

## Material forbidden to reuse

| ID | Classification | Material | Rule |
| --- | --- | --- | --- |
| FR-01 | `FORBIDDEN_TO_REUSE` | Source code, control flow, algorithms, constants, exact retry timing, or exact startup sequences from the pinned repository | Must not be copied, translated, mechanically transformed, or reconstructed line by line. |
| FR-02 | `FORBIDDEN_TO_REUSE` | Type declarations, interfaces, event shapes, configuration schemas, generated protocol files, or tests | Must not appear in Cyrene source or test fixtures. |
| FR-03 | `FORBIDDEN_TO_REUSE` | Native hooks, binary loaders, packet implementations, private protocol details, symbol names, or platform bypass techniques | Must not be used as design or implementation input. |
| FR-04 | `FORBIDDEN_TO_REUSE` | Native binaries, packaged assets, vendored dependencies, or release archives | Must not be redistributed, linked, loaded, or declared as a Cyrene dependency. |
| FR-05 | `FORBIDDEN_TO_REUSE` | NapCat process IPC, named-pipe protocol, supervisor messages, filesystem layout, or internal module naming | Cyrene must define its own worker protocol and lifecycle. |
| FR-06 | `FORBIDDEN_TO_REUSE` | NapCat configuration or account data | Must not be imported, migrated, read, or reinterpreted by `qqnt-direct`. |

## Research conclusion

The permitted observations support only broad lifecycle requirements: version
discovery, ordered login/session initialization, event-driven messaging,
process supervision, and strict account isolation. They do not supply an
implementation boundary that Cyrene is authorized to reuse. The clean-room
runtime must therefore be specified from Cyrene contracts, Tencent-authorized
interfaces, public protocol facts, and independently created black-box tests.
