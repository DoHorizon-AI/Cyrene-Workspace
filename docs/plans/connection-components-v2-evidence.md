# Connection components V2: execution evidence

Status: **SOURCE IMPLEMENTED; DEPLOYMENT ACCEPTANCE PENDING**. This log distinguishes source delivery, local checks, and deployment acceptance. Refer to [the implementation plan](connection-components-v2.md) for the acceptance checks.

## Immutable owner catalogs

The following commits contain only the V2 operation catalog and its contract index. They remain the immutable inputs to the Product contract bundle. All six source branches have now been normally merged into `develop`; subsequent Dockerfile and verified-OCI delivery changes are recorded separately below. Catalog-source pins and final build-source pins serve different purposes.

| Repository | Catalog source commit | Hosted CI / integration |
| --- | --- | --- |
| Catalyst | `75ad5ded966f66609f2b016fb96c62be1511ef02` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Catalyst/actions/runs/36927346542); [PR #14 merged](https://github.com/DoHorizon-AI/Cyrene-Catalyst/pull/14) |
| Yield | `4fc49155786e594ec5f67e5c8f42ec5b35cdd9ab` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Yield/actions/runs/36927350192); [PR #17 merged](https://github.com/DoHorizon-AI/Cyrene-Yield/pull/17) |
| Reactor | `c8f11c09b4f873bf1a550e83a528c5ea05c0b446` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Reactor/actions/runs/36927355529); [PR #15 merged](https://github.com/DoHorizon-AI/Cyrene-Reactor/pull/15) |
| Exchange | `8a6258e656d90bc8fcc22ce570b61917fad7763a` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Exchange/actions/runs/36927360468); [PR #20 merged](https://github.com/DoHorizon-AI/Cyrene-Exchange/pull/20) |
| Echo | `4de40bd9e50f3048491b00e868f7767911237b63` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Echo/actions/runs/36927363424); [PR #14 merged](https://github.com/DoHorizon-AI/Cyrene-Echo/pull/14) |
| Navigator | `8224a47551533c1d17857d418ff63dc1857b8b2d` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/36927832527); [PR #11 merged](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/11) |

The catalog worker reports that all six catalogs pass Draft 2020-12 validation, the existing thirteen-operation projection comparison, unique OpenAPI route resolution, schema-pointer resolution, route-parameter coverage, and scope-binding checks. These catalog checks are static evidence. The executable contract and authorization checks below provide additional source evidence; deployed dispatch remains pending. Navigator append remains catalogued without a Platform policy grant.

## Trusted update metadata

Workspace commit `522524b86a8f79c70e948fed8cb27c4cc70f29c4` is the current generation-2 catalog authority. Its raw SHA-256 is `248a9a3b27f3d1daa4c0a6fdc405c612fd492483bb4157ff46b6d2836ffd0d35`. [Exact-commit CI passed](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/36933536469). This supersedes the initial metadata commit and adds the SDK/runtime dependencies and fixed activity-source catalog path.

The original single-service bundle builder was committed separately at `ae34998afab7457d864c29cd6dffdc860fa07c55`; [its original exact-commit CI passed](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/36936658521). The initial CI did not cover all new producer paths. The missing wheel-METADATA helper was repaired at `61ac49b8bd3b00d6db1b2f92f4170e62922a0e25`; formatting, executable Git mode and the new component-updates CI gate were finalized at `cec19d36ab7eaeb10daada52dc5c1e9166ea30d0`. All six Product workflows now pin that verified builder source. The exact-source [push CI](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/36949091813) and [PR CI](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/36949096040) passed, including 65 focused updater/identity/builder/CLI tests, Ruff format/lint, shell syntax and release-schema/catalog validation. Source artifact upload depends on these gates. New staged service bundles require schema v2; legacy v1 is read-only compatibility. Source CI and synthetic wheel fixtures do not prove a published component.

## Local source checks

These checks provide local source and scoped runtime evidence. The delivery record below separately names final integration commits and hosted CI.

| Area | Observed result | Limit |
| --- | --- | --- |
| Generic contracts | Final locked offline Cargo: 7 unit and 4 dynamic-operation integration tests passed; strict Clippy passed. The separately configured real six-owner bundle smoke passed; Python bundle suite: 12 passed. | The final ordinary Cargo run skipped the bundle smoke when its generated-bundle environment was absent. No deployed dispatch is implied. |
| Control plane | Locked offline Cargo: 57 passed. Unknown/unauthorized operations, recursive duplicate JSON keys and scope/pin enforcement are covered. | Deployed dispatch remains pending. |
| HTTP adapters | Locked offline Cargo: 14 passed, including route binding, payload limits and slow-response timeout. | No remote Product request has been accepted yet. |
| Client V2 and update UI | Node 24.21.0: full TypeScript check, 190 Vitest tests and Web build passed; 6 tests were skipped. Playwright settings: 8 passed, including explicit local mode and V2 failure without local fallback. Final Windows installer: 24 Rust tests, Linux workspace check and MSVC target check passed. | Linux helper/CLI source is integrated; real host-root Polkit and Windows Docker Desktop update remain unrun. |
| Product activity hooks | Product worker reports 86 targeted tests passed, plus scoped lint/format/type checks. The five activity-owning Products have green exact-SHA hosted CI; Navigator's catalog CI is also green. Six Dockerfiles passed Buildx configuration checks. | No actual SDK-bearing image build or released-component update is included in this result. |
| Core update gate | A real Core v2 gRPC Unix socket test passed with peer credentials, Lease/Worker blockers, concurrent admission versus maintenance, persistent reopen and rollback unlock. | Hardware and sandbox providers are simulated; this is not GPU runtime acceptance. |
| Broker and SDK | Strict Clippy passed for broker and Kernel/Core. Broker: 8 unit tests passed; daemon: 86 unit, 1 Core UDS integration and 16 TCK tests passed in a user namespace. Real Unix-socket task-fixture and durable Begin/End receipt acceptance passed, including restart, generation and token recovery. | Task workloads and release provenance in local update acceptance are fixtures. Host-root system-unit acceptance remains unrun. |
| Native composition | Relay host compiled and passed 9 targeted tests; native XFCC rejection passed 2 tests. BFF compiled without a BuildKit build context and requires configured production identity inputs. Connector built and passed 2 tests in its real Dockerfile builder. All official PostgreSQL migrations and nine restricted application-login TLS connections passed. A real restricted CA signer persisted and verified a signed empty CRL. | Human AAD/WebAuthn approval and two-host dispatch are unrun. |
| Native transport, local | The real Relay reported all six readiness dependencies healthy before and after probes. TLS 1.3/h2 rejected missing client certificates with `CertificateRequired` and an unrelated CA with `UnknownCA`. Actual Tonic `Connect` confirmed both transport failures with retained transport error chains; a valid pinned workload certificate reached the application, and spoofed XFCC was rejected. | Test workload identity on one host only. Device issuance, approval ACK, Active registry transition, revocation, reconnect, Product dispatch and Sidecar remain unrun. |
| Linux updater identity | 65 focused tests passed in hosted CI. Final frozen-source real-broker/systemd-user run `dd70178d178219a5` exited 0 and verified busy stage, explicit apply, race rejection/retry, same-version/different-digest updates, exact unhealthy/interrupted rollback, and unmanaged-unit refusal without Begin or switching. | GitHub provenance, catalogs and payloads are simulated; the workload is a CPU SDK fixture and the real unit belongs to the user manager. Host-root DEB/Polkit and GPU/Product acceptance remain unrun. |

The Product lock references all six catalog commits above and pins the policy and bundle digests. Navigator append has no approved policy grant. Local update races and recovery are recorded separately below; remote connection and actual user/device approval remain pending.

## Source integration and read-back

The final installation inputs below include the activity hooks, generic V2 contracts, immutable-release consumers and scoped settings-read credential hook. The eight component repositories were normally merged into `develop`; fetched refs and direct remote reads match these merge commits. Workspace records its validated source predecessor in `release-lock.json`: a commit cannot contain its own SHA. The final Workspace merge, source checks and read-back are recorded by [PR #27](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/27) and the local delivery record `/tmp/cyrene-connection-components-v2-final-delivery.json` after this evidence commit is integrated.

| Repository | Final `develop` input | Normal integration / hosted source CI |
| --- | --- | --- |
| Platform | `bbbafeeb242c28555b47b0bfa2c8e74e618a2c3f` | [PR #67](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/67), [PR #68](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/68); [CI](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/36951296436), [Workspace Fabric](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/36951296394) passed |
| Catalyst | `074d0b38d616946793800f2e9db212e9c519d404` | [PR #17](https://github.com/DoHorizon-AI/Cyrene-Catalyst/pull/17); [source tests](https://github.com/DoHorizon-AI/Cyrene-Catalyst/actions/runs/36951133771), [contracts](https://github.com/DoHorizon-AI/Cyrene-Catalyst/actions/runs/36951133874) passed |
| Yield | `19d25f37cb02c07dac574aef7ec98118e92ada89` | [PR #20](https://github.com/DoHorizon-AI/Cyrene-Yield/pull/20); [source tests](https://github.com/DoHorizon-AI/Cyrene-Yield/actions/runs/36951296553), [training](https://github.com/DoHorizon-AI/Cyrene-Yield/actions/runs/36951296570), [contracts](https://github.com/DoHorizon-AI/Cyrene-Yield/actions/runs/36951296673) passed |
| Reactor | `8d3cec973bc51c21a07bb5bd8dec1f9c596f7069` | [PR #18](https://github.com/DoHorizon-AI/Cyrene-Reactor/pull/18); [CI](https://github.com/DoHorizon-AI/Cyrene-Reactor/actions/runs/36951187544), [contracts](https://github.com/DoHorizon-AI/Cyrene-Reactor/actions/runs/36951187569) passed |
| Exchange | `0bec57a5fc593d7eb3ae1e71f38bf0b6dd8ab22b` | [PR #22](https://github.com/DoHorizon-AI/Cyrene-Exchange/pull/22); [CI](https://github.com/DoHorizon-AI/Cyrene-Exchange/actions/runs/36951158361), [contracts](https://github.com/DoHorizon-AI/Cyrene-Exchange/actions/runs/36951158358) passed |
| Echo | `7480d72c3d2520ccf7684ae43e2f2558c5c6e828` | [PR #17](https://github.com/DoHorizon-AI/Cyrene-Echo/pull/17); [source tests](https://github.com/DoHorizon-AI/Cyrene-Echo/actions/runs/36951335179), [contracts](https://github.com/DoHorizon-AI/Cyrene-Echo/actions/runs/36951335368) passed |
| Navigator | `73a0b56e11c24a67d740a262334a2ea78dee96f0` | [PR #13](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/13); [native/Python CI](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/36951582390), [Windows packaging](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/36951582393), [contracts](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/36951582449) passed |
| Client | `d04c310fffdcfc82976dec8bbadedecfd07608d8` | [PR #12](https://github.com/DoHorizon-AI/Cyrene-Client/pull/12); [CI](https://github.com/DoHorizon-AI/Cyrene-Client/actions/runs/36943369018), [MSIX](https://github.com/DoHorizon-AI/Cyrene-Client/actions/runs/36943369068), [CD image](https://github.com/DoHorizon-AI/Cyrene-Client/actions/runs/36943369049) passed |
| Workspace | Validated predecessor `cec19d36ab7eaeb10daada52dc5c1e9166ea30d0` | [PR #27](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/27); [predecessor CI](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/36949096040) passed; final-commit checks are attached to the PR |

The canonical Platform checkout was fast-forwarded without touching its five Gemini files: before/after content hashes and status match. Yield's ten Gemini paths remain in their original dirty checkout; integration used a separate clean worktree, and `origin/develop` was read back independently. No unrelated work was staged or reverted.

The release workflows and SDK-aware Dockerfiles are now integrated in all six Product sources. Catalyst, Yield, Reactor, Echo and Exchange also use verified immutable OCI releases in their existing Azure delivery workflows. Those consumers require an exact source-commit release and attested SDK identity, and update an existing app by digest; they do not build from mutable sibling sources or create Azure resources. A green source artifact-upload job is not an immutable component release. Repository Immutable Releases settings have been proposed for the seven publishers and await explicit authorization.

The dedicated `CYRENE_IMMUTABLE_RELEASE_SETTINGS_READ_TOKEN` hook is scoped to the single settings-preflight step in Platform and all six Product workflows. It is required only for the repository-settings GET; ordinary content queries and publishing retain their workflow token. No secret was configured or repository setting changed. Platform [release run 36951296291](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/36951296291) failed closed because this secret is unset, and all downstream build/attestation/publication steps were skipped. The six Product publishers are separately `release-dependency-blocked`: SDK bootstrap has no trusted immutable Platform preview index containing the Runtime Maintenance SDK, so their settings-read steps were not reached. This does not identify a Product settings-auth failure. Reading this GitHub setting requires repository Administration read permission; see the [official endpoint contract](https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository).

Client PR [#11](https://github.com/DoHorizon-AI/Cyrene-Client/pull/11) was normally merged into `develop` at `83b0e1d5fb369ef5d99b258e10019bde332f463c`. The later maintenance refusal/retry fix was normally merged through [PR #12](https://github.com/DoHorizon-AI/Cyrene-Client/pull/12) at `d04c310fffdcfc82976dec8bbadedecfd07608d8`. The final exact merge-commit [Client CI](https://github.com/DoHorizon-AI/Cyrene-Client/actions/runs/36943369018), [MSIX packaging](https://github.com/DoHorizon-AI/Cyrene-Client/actions/runs/36943369068) and [CD image build](https://github.com/DoHorizon-AI/Cyrene-Client/actions/runs/36943369049) passed; canonical checkout, remote `develop` and source ancestry were read back. Azure login, deployment and health steps were skipped.

The broker used for local receipt acceptance had SHA-256 `2cd518f24a70687ac8a8979445022ae631d0560930442916cc509cb018b503c7`. After the Core wire enum zero name was corrected for Buf, the final rebuilt broker has SHA-256 `c9dde94ae96ca8df26399742420d0a99737f404f5f62aeb6bc4f2f8f5f566275`; the domain `UNKNOWN` status is unchanged. These are local executable digests, not attested published components. Updaters must send the original Begin transaction ID in End `params.request_id`; the top-level JSONL request ID is a separate correlation ID. Known normal Begin refusals without a token restore the staged plan; malformed or transport-unknown outcomes retain durable recovery intent.

Existing Product installations without the maintenance SDK report unknown activity and cannot enter an unattended update. Stopping a container is not proof that its persisted tasks are complete. Initial migration needs an independently verified maintenance window; the installer does not infer idle or restart existing Products to bootstrap the broker.

## Supported installation paths

| Platform / component | Implemented path | Acceptance boundary |
| --- | --- | --- |
| Linux Products | Installer-managed Navigator, Catalyst, Yield, Reactor and Exchange Python bundles; outer verified receipts bind the inner bundle identity. | Immutable published SDK-bearing payloads and host-root installation remain unrun. Echo has no Linux Python installation entry. |
| Linux native hosts | System/GPU adapters, sandboxd, Kernel, Node Agent, maintenance broker and Workspace Relay/BFF/Connector/Sidecar use digest-resolved `cyrene component-run` entry points. Native artifacts include trusted systemd units; only absent units are installed. | Rootless user-manager switching is tested. Host-root unit/DEB/Polkit execution remains unrun. Kernel no longer declares a broad `StateDirectory=cyrene`, which could recursively take over broker state ownership. |
| Runtime Agent Worker | Its actual existing launch path is an OCI Worker image selected by the execution provider, with a new Runtime generation and a matching resolved image/assignment digest. | No deployed host-systemd consumer or OCI update-management API exists. A native payload pointer cannot update the Worker image. Apply fails before Begin/switch when the trusted managed unit is absent; no dummy unit is created. |
| Windows Products | Existing Exchange, Reactor, Yield, Catalyst and Echo containers use verified OCI digests and the same explicit maintenance protocol. | Installer source checks passed; actual Docker Desktop update and recovery remain unrun. Navigator has no Windows OCI entry. |

Normal SDK dependencies exclude SQL, WebAuthn and Platform server implementation crates. Sidecar retains its own Tonic/HTTP local IPC host dependencies. Dependency-tree captures have SHA-256 `4a8ef9d34d8c04de8081f6f6b4304946c084f2e7de971c5d890b830ee014f6d9` (SDK) and `aca457ff5c80c05f4097d2a8b511b92febb9c183ffcab353b2f67eabcbf0b6bc` (Sidecar). Rust shared-library changes still rebuild affected executables; deployment switches only the affected component or a validated compatibility group.

The local managed-unit check requires the catalog's `systemdUnit` and restart unit to agree, a root-owned regular unit file without group/world write access, and the trusted component-run entry point. Status can retain an OS-supported target while allowing only check/stage and reporting `SERVICE_NOT_MANAGED`; this does not report the absent service as updateable.

## Frozen-source local updater acceptance

Run `dd70178d178219a5` exited `0` with result `PASS_WITH_SIMULATED_GITHUB_PROVENANCE_AND_ROOTLESS_USER_SYSTEMD_SCOPE`, using Workspace source `cec19d36ab7eaeb10daada52dc5c1e9166ea30d0`. The updater source SHA-256 is `6446438f21db858fff186a787bf66c4976b9265e17899a832839af297e7d1832`; the broker binary SHA-256 is `c9dde94ae96ca8df26399742420d0a99737f404f5f62aeb6bc4f2f8f5f566275`. The report is retained locally at `/tmp/cyrene-component-local-acceptance/runs/dd70178d178219a5/final-report.md`, SHA-256 `82911af8baf93b457de44513e33e57b6ff74d5a1005ad21b8eb9f8493a76f751`; its machine summary is `evidence/final-verification.json`, SHA-256 `e150b3303728eccbb4aef96b2de39a308cf7552f68ea1e3a9de33ad7cffd99aa`.

The actual command was:

```sh
unshare -Ur python3 /tmp/cyrene-component-local-acceptance/acceptance_runner.py \
  --broker-bin /tmp/cyrene-components-target/debug/cyrene-runtime-maintenance
```

Real UDS peer credentials, SDK task lifecycle and a random systemd user-manager unit were exercised. Namespace UID/GID maps were `0 1000 1`; namespace root was not host root. A 2 MiB CPU task from a temporary `cyrene-echo` activity source blocked apply while allowing stage. The updater component target was a synthetic `cyrene-linux-sys-adapter`, not Echo's unsupported Linux installation path. The test injected temporary catalog/release/attestation and managed-unit roots; trusted unit-file validation ran on a real temporary file, while updater service commands were mapped to the real user manager. BFF/Connector startup contract-root checks used captured `execve`.

Six Begin calls produced five acquired maintenance receipts and one `STALE_READINESS` refusal; five End calls returned `READY`. The final five transaction records were three `succeeded` and two `rolled_back`, with no pending intent or retained token. A same-artifact check offered no update; a same-version new digest used a distinct release directory. Killing after durable `applying` plus switching and applying an unhealthy candidate both restored the exact prior pointer/digest. Task completion did not auto-install, and the race loser required a new explicit retry.

The missing Runtime Agent unit returned `SERVICE_NOT_MANAGED` before any new Begin, pointer change, transaction or dummy unit. Its separate Core readiness remained `UNKNOWN/BROKER_UNAVAILABLE` because the fixture did not start a Kernel UDS backend; this is a rejection check, not Core readiness acceptance. Cleanup independently confirmed the random user unit was `not-found/inactive` with an empty fragment path, no run-owned processes remained, and broker credentials/state/catalog/socket were removed. Earlier failed harness attempts remain recorded as fixture/expectation corrections and are not counted as acceptance.

## Local acceptance infrastructure

At 2026-10-01 20:58 UTC, the isolated PostgreSQL 17 container `cyrene-components-v2-postgres` passed:

```sh
docker exec cyrene-components-v2-postgres \
  pg_isready -U cyrene_acceptance_operator -d cyrene_components_v2
```

It listens only on `127.0.0.1:44626` and uses the task volume `cyrene-components-v2-postgres-data`. Its private configuration is outside component version directories. The existing `cywsauthdebug` database and volume were retained. A real `psql` connection with `sslmode=verify-full` reported `TLSv1.3`; a deliberately mismatched hostname was rejected. The isolated database TLS configuration follows [PostgreSQL 17 server TLS](https://www.postgresql.org/docs/17/ssl-tcp.html) and [client verification](https://www.postgresql.org/docs/17/libpq-ssl.html). Database readiness and TLS alone do not prove issuance, approval, ACK, registry persistence, or revocation; those checks remain pending and must use restricted application roles.

## Deployment checks awaiting access

| Check | Current evidence | Remaining work |
| --- | --- | --- |
| CCV2-REMOTE | The tailnet host is reachable; SSH host-key validation passed, but login returned `Permission denied (publickey)`. The user confirmed that the desktop lacks the required key and will arrange it. | Native two-host enrollment, Relay dispatch, revocation, reconnect and update acceptance. |
| CCV2-WINDOWS | Docker works in the local WSL environment; the Docker Desktop context is unavailable in this session. | Real Windows installer, Docker Desktop update and rollback. |
| CCV2-LINUX | Real broker/SDK/user-systemd acceptance passed with simulated release provenance and a CPU task fixture. Native payloads include trusted host units and source guards. | Host-root DEB/Polkit, real immutable payloads and Product/GPU runtime group acceptance. Runtime Agent remains an OCI Worker consumer and has no native managed host unit. |
| CCV2-DELIVERY | Platform PRs #67/#68, six Product delivery follow-ups and Client PRs #11/#12 are integrated with source CI and remote read-back. Final installation inputs and local evidence are included in Workspace PR #27. Gemini edits remain preserved. | Final Workspace commit checks, normal merge and read-back are recorded on PR #27; immutable publication separately needs repository-settings authorization and the dedicated read credential, followed by trusted SDK publication. |

No SSH keys have been moved, and no Azure resources have been created. Remote and Windows deployment checks remain **NOT RUN**. Continue local implementation and acceptance while access is being prepared.

---
<!-- Chinese Translation / 中文翻译 -->

# 连接组件 V2：执行证据

状态：**源码已实现，部署验收待完成**。本记录分别说明源码、静态检查与真实部署验收；验收条目见[实施计划](connection-components-v2.md)。

六个 Product 的 v2 操作目录、契约索引、任务门禁、SDK-aware Dockerfile 与组件发布流程已分别正常合并到 `develop`。目录 source SHA 继续作为不可变 bundle 输入，后续构建 source SHA 另行固定；目录与构建版本用途不同。Catalyst、Yield、Reactor、Echo 和 Exchange 的既有 Azure CD 也已改为读取验证后的 OCI digest，仅更新现有 app。本轮未执行 Azure 更新。目录静态 schema、十三项操作对照、OpenAPI 路由唯一性、schema 指针、参数覆盖及作用域绑定检查通过。本机通用契约最终 7 项单测和 4 项动态操作测试、严格 Clippy、控制面 57 项及 adapter 14 项测试通过；单独配置环境的六仓 bundle smoke 也通过，常规测试中未配置该环境时明确跳过。它们提供源码验证，尚不代表部署后的真实调用验收。Navigator append 没有 Platform 策略批准，继续默认拒绝。

Workspace 当前受信目录为 generation 2，authority 为 `522524b86a8f79c70e948fed8cb27c4cc70f29c4`，摘要见英文段落，保持原目录 authority。初始 builder `ae34998afab7457d864c29cd6dffdc860fa07c55` 的原 CI 未覆盖所有新路径；新增 CI 发现的 wheel-METADATA helper 与 Git 可执行权限问题已修复，六个 Product 统一固定完整检查通过的 builder source `cec19d36ab7eaeb10daada52dc5c1e9166ea30d0`。其精确 push/PR CI 全绿，新 component-updates job 覆盖 65 项更新器/身份/builder/CLI 测试、Ruff 格式与 lint、shell 语法和 schema/catalog 检查；源码 artifact 上传依赖这些检查。定向测试使用真实 ZIP 格式的合成 wheel fixture，并非正式 SDK 发布产物。新 stage 只接受 v2，旧 v1 bundle 仅供读取与回滚兼容。

Client 在 Node 24 下通过 190 项测试（6 项跳过）、TypeScript 检查和 Web 构建；Playwright 设置流程 8 项通过，覆盖明确本机模式及 V2 失败后不回退本机请求。最终 Windows 安装器 24 项 Rust 测试、Linux workspace 与 MSVC 目标检查通过。PR #11 与维护拒绝重试修复 PR #12 已正常合并到 develop，最终精确合并提交的 Client CI、MSIX 打包及镜像 CD 通过，完成 canonical 读回。CD 只构建推送镜像，Azure 登录、部署及健康检查均跳过。Product 活动门禁定向测试共 86 项通过，Dockerfile 仅完成配置检查。真实 Core v2 Unix socket 测试覆盖 Lease/Worker 阻断、准入与维护并发、持久恢复及回滚解锁，硬件和 sandbox provider 为模拟实现。Broker/Kernel/Core 严格 Clippy 通过，broker 8 项单测、daemon 86 项单测、1 项 Core UDS 集成与 16 项 TCK 通过。真实持久 Begin/End receipt 和最终 systemd 用户服务更新验收通过，主机 root 系统单位仍未执行。

Native Relay、BFF 和 Connector 的编译及定向测试通过；隔离 PostgreSQL 下的全部官方迁移以及 9 个受限应用登录的 TLS 连接通过。真实受限 CA signer 已持久化并验证签名空 CRL。本机真实 Relay 在探针前后均就绪：TLS 1.3/h2 明确拒绝无证书与错误 CA，真实 Tonic Connect 保留对应 transport error chain；有效 pinned workload 证书到达应用层，伪造 XFCC 被拒绝。这仅是单机测试 workload 的传输验收，真实用户 AAD/WebAuthn、设备审批 ACK、Active 转换、撤销、重连及 Product dispatch 尚未执行。Linux 最终源码验收 run `dd70178d178219a5` 退出码 0，通过真实 broker/SDK/systemd-user 的忙态暂存与拒绝、任务终态不自动安装、精确摘要确认、同版本不同产物、准入竞态及显式重试、中断与不健康产物精确回滚；5 个最终事务为 3 成功、2 回滚，没有 pending intent/token，临时用户服务、进程和 broker 凭据已清理。GitHub provenance、catalog 和产物仍为模拟，任务为 2 MiB CPU SDK fixture，未执行 root DEB/Polkit、GPU/Product 实际任务或双机流程。无 Runtime Agent 单位时在 Begin 前返回 `SERVICE_NOT_MANAGED`；其 Core readiness 因未启动 Kernel UDS 保持 `UNKNOWN/BROKER_UNAVAILABLE`，不计为 Core 验收。End 的 params.request_id 使用原 Begin 事务 ID，顶层 JSONL request_id 仅作为关联 ID；已知无 token 的正常拒绝恢复 staged 计划，格式异常或传输结果不确定时保留持久恢复意图。

Linux 五个 Python 服务与 Windows 五个 OCI 服务范围见英文支持表；Echo 不提供该 Linux Python 安装路径，Navigator 不提供 Windows OCI 路径。Runtime Agent 现有消费者使用 OCI Worker 镜像；native active 指针不改变该镜像，本轮没有部署中的 host-systemd consumer 或 OCI 更新管理 API，不能把单独 native 文件切换计为 Worker 升级。Kernel systemd 的 broad StateDirectory 已移除，避免递归接管 root/authority journal 与私有 token 子树。SDK/Sidecar 普通依赖不包含数据库、WebAuthn 或 Platform server 实现；Sidecar 自身本机 IPC 的 Tonic/HTTP 依赖保留。Rust 共享库变化仍需重建受影响可执行组件，部署只切换受影响组件或兼容组。

八个组件仓的最终 develop 提交、合并 PR 和源码 CI 见英文交付表；Platform PR #67/#68、六个 Product follow-up 和 Client PR #11/#12 已正常集成并读回。release-lock 固定这些最终输入，Workspace 记录完整验证过的 source predecessor，避免提交自引用；本次最终合并与 CI 在 Workspace PR #27 和本地最终交付 JSON 中记录。Platform 五个与 Yield 十个 Gemini 路径完整保留，未混入本轮提交。

七个发布仓的 Immutable Releases 设置变更仍等待明确授权；正式发布另需专用 `CYRENE_IMMUTABLE_RELEASE_SETTINGS_READ_TOKEN`，只在 settings-preflight 单步用于读取仓库不可变设置，普通内容查询和发布继续使用 workflow token，未写入真实 secret 或修改设置。Platform 合并后发布 run 36951296291 因该 secret 未配置而拒绝，后续构建、attestation 和发布步骤全跳过。六个 Product 发布则先阻断在 SDK bootstrap：尚无包含 Runtime Maintenance SDK 的受信不可变 Platform preview index，未到达 settings-read 步骤，因此不能归类为 Product 设置权限失败。源码 CI 与 artifact-upload 通过不等于组件正式发布。

旧版 Product 未接 maintenance SDK 时报告活动未知，不能无人值守安装。停止容器不能证明持久任务已完成；首次迁移需另行验证维护窗口，安装器不会推断空闲或重启既有 Product 来初始化 broker。

本机已建立独立 PostgreSQL 17 验收容器，2026-10-01 20:58 UTC 的 `pg_isready` 返回可接受连接。它只绑定本机 loopback，使用独立持久卷，密钥配置置于组件版本目录之外；旧数据库未改动。真实 `psql` 连接以 `verify-full` 验证主机身份，连接使用 `TLSv1.3`，错误主机名被拒绝。配置参照 PostgreSQL 17 官方服务端 TLS 与客户端验证文档。签发、审批、证书 ACK、持久 registry 和撤销仍待验收，应用必须使用受限数据库角色。

远端主机可经 tailnet 访问，SSH 主机密钥校验通过，但登录缺少可用密钥。用户正在准备迁移密钥，双机验收保持未执行。当前 WSL 能使用 Docker，Windows Docker Desktop 上下文不可用，Windows 真实安装及回滚也保持未执行。继续完成本机实现与验收，不把待执行项目计为通过。

尚未迁移 SSH 密钥，未创建 Azure 资源。源码交付按上表和 Workspace PR #27 记录，真实部署项目保持待执行；本轮不宣称公网免端口或 ACA 生产部署验收。
