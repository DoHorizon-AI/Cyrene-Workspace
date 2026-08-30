# QQNT Direct Compatibility and Risk Matrix

## Compatibility matrix

| Dimension | Initial position | Evidence required | Failure behavior |
| --- | --- | --- | --- |
| Capability | `message.connector.v1@1` | Existing contract/TCK and CES binding tests | Reject incompatible Platform API. |
| Connector family | `qq` | Package descriptor and PluginManifest projection | Reject missing or conflicting family metadata. |
| Runtime profile | `qqnt-direct` | Explicit binding configuration | Never infer from an endpoint or first available profile. |
| Compatibility profile | `onebot-v11` remains available in the same package | Existing OneBot TCK and package lifecycle gate | Direct mode never falls back to OneBot. |
| Windows x64 QQ | Candidate first native placement, subject to approval | Exact official installer, QQ build, ABI, terms, and native-interface approval | Unknown build fails closed. |
| Linux x86_64 QQ | Discovery/design candidate only | Exact official deb/rpm/AppImage source, display/session needs, ABI, and terms approval | Unsupported until separately admitted. |
| Other Linux architectures | Not in first slice | Official package availability plus independent native compatibility evidence | Unsupported. |
| WSL Platform + Windows QQ | Preferred desktop split if stdio ownership is reliable | End-to-end process reaping, path canonicalization, secret placement, stdio behavior | Named pipe fallback or unsupported; no TCP fallback. |
| Native IPC | Inherited stdio | Framing, cancellation, backpressure, malformed input, process ownership tests | Fail activation if unavailable. |
| IPC fallback | Owner-only UDS or Windows named pipe | Permission and lifecycle tests | No localhost listener fallback. |
| Account model | One QQ account/session per binding process tree | Dual-binding isolation and restart tests | Account mismatch fails binding activation. |
| Package reuse | One immutable artifact/runtime shared read-only by digest | Package-installed vertical gate | Reject digest mismatch or source fallback. |
| Mutable state | Binding-private | Filesystem, secret, event, queue, and process isolation tests | Stop affected binding; never borrow peer state. |
| Login | Existing official session or QR | Authorized interface and dedicated-account smoke | No password/captcha bypass fallback. |
| Message scope | Private/group text | Independent mapping tests and official-runtime smoke | Unsupported content returns deterministic error. |
| Rich media | Deferred | Separate media lifecycle, dependency, and legal review | Explicitly unsupported in first slice. |

## Risk matrix

| Risk | Level | Current evidence | Required mitigation / decision | Owner gate |
| --- | --- | --- | --- | --- |
| NapCat license risk | Critical if reused; Low when excluded | Pinned repository uses a restrictive custom license with non-commercial and redistribution conditions. | Zero code/types/tests/hooks/schemas/binaries/runtime dependency; exposed researcher does not implement native wrapper; provenance review. | Legal + clean-room reviewer |
| QQ platform/terms risk | Critical | Tencent publishes QQ software terms, but this audit found no clear authorization for third-party native wrapping or automation. | Written approval for exact technique and test-account use; reject injection/reverse-engineering/redistribution unless specifically authorized. | Product owner + counsel |
| Technical version coupling | High | Observable implementations depend on QQ build, platform layout, architecture, and native compatibility. | Exact compatibility allow-list, discovery report, pinned test image, fail closed, rapid rollback. | Runtime maintainer |
| Windows/WSL process placement | High | QQ may live on Windows while Platform runs in WSL; process ownership, paths, signals, and secrets cross OS boundaries. | Inherited stdio proof, explicit Windows process-tree ownership, canonical paths, native-side account state; named pipe only if needed. | Platform/runtime maintainer |
| Native library distribution | Critical | Official QQ packages are downloadable, but redistribution rights for their libraries were not established. | Discover separately installed official QQ; never bundle libraries without written redistribution authority. | Release + legal |
| Clean-room provenance | High | Architecture author was exposed to high-level source structure during restricted research. | Separate implementation owner, input log, independent naming/design, diff review, no reference checkout in implementation/CI. | Clean-room reviewer |
| Undocumented native interface | Critical | No Tencent-published stable desktop automation/native event API was established. | Obtain authorized interface or stop. Do not substitute private hooks/protocol extraction. | Architecture + legal |
| Account restriction/risk control | High | Official upstream security guidance warns of account restrictions and disconnect behavior. | Dedicated accounts, isolated environment, bounded traffic, kill switch, no circumvention. | Operations + product owner |
| Secret/session leakage | High | Two bindings share package code but must not share mutable account state. | Binding-private directories and secret handles; one process tree/session per binding; adversarial overlap tests. | Security + runtime maintainer |
| Cross-instance routing | High | Shared capability and implementation can coexist under multiple bindings. | Binding stamped at ingress, immutable worker startup binding, correlation scoped by binding/generation, CES explicit selection. | Platform + connector maintainer |
| Hidden OneBot fallback | High | Same package contains a OneBot compatibility profile. | Mutually exclusive config branches; direct mode rejects endpoint fields; test with no OneBot endpoint and source unavailable. | Connector maintainer |
| Listening-port regression | Medium/High | Network adapters are common in compatibility runtimes. | Socket audit for startup/login/send/restart/shutdown; default profile permits only stdio or local owner-only IPC. | CI owner |
| Native crash/orphan process | High | Native runtimes and cross-OS helpers may not terminate with their parent automatically. | Process-group/job ownership, bounded graceful shutdown, forced reap, post-test orphan/IPC/lock checks. | Runtime maintainer |
| Message/media data handling | Medium for text; High for media | Text mapping is bounded; media adds upload/download/temp lifecycle and native dependencies. | Text-only first slice; separate media design with retention and attachment isolation. | Connector + security |
| Package supply chain | High | Native helper is privileged local code and package lifecycle is dynamic. | Immutable artifact, exact lock, digest, optional signature/SBOM, verified cache, no install-latest, upgrade/rollback gate. | Release + security |

## Risk decisions

### NapCat license

Risk is controlled only by exclusion. NapCat is a research reference, not a
component, dependency, test fixture, or compatibility runtime. Any request to
reuse its implementation reopens legal review and invalidates the current
clean-room plan.

### QQ platform and terms

This is the principal blocker. Public availability of a QQ installer does not
imply permission to wrap internal APIs, automate a user account, modify the
process, or redistribute native libraries. Implementation readiness remains
`NO` until the exact boundary is authorized.

### Version coupling

The runtime is admitted per exact QQ build/platform/architecture tuple. A
package version records the supported tuple and native-helper ABI. Automatic QQ
updates must move a binding to `UNAVAILABLE_VERSION` until compatibility is
proven; they must not trigger an arbitrary best-effort load.

### Windows/WSL placement

The preferred desktop arrangement keeps CES and the generic worker authority in
WSL while placing only the native helper/session beside Windows QQ. Inherited
stdio is the first transport. Windows named pipe is the bounded fallback. If
neither provides reliable ownership and cleanup, the profile is unsupported in
that topology.

### Native distribution constraints

The Cyrene package distributes only Cyrene-owned code and locked open-source
dependencies with compatible licenses. The official QQ runtime is installed
separately from Tencent's official channel. QQ libraries, assets, account data,
and update payloads are not copied into the package or offline cache.

### Clean-room provenance

Research and implementation roles are separated. The implementation record
must show independent origin for native APIs, protocol framing, state machine,
tests, and constants. Unexplained structural similarity is a release blocker.

## Current decision summary

- NapCat code reuse: **NO**.
- NapCat runtime dependency: **NO**.
- Portless `qqnt-direct` architecture: **FROZEN**.
- Package/profile design: **FROZEN**, subject to ordinary manifest/schema review.
- Clean-room native implementation: **NOT READY** until QQ terms and an
  authorized native interface are recorded.
