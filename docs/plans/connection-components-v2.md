# Connection components and controlled updates V2

Status: **SOURCE IMPLEMENTED; DEPLOYMENT ACCEPTANCE PENDING**. Checkboxes below require actual evidence; a source change or unit test alone does not complete a deployment check.

Current source and environment evidence is recorded in [the execution log](connection-components-v2-evidence.md).

## Authority and compatibility

Platform retains identity, Directory, authorization, device binding, revocation, dispatch fencing, and the Authority/BFF hosts. Plugins owns the independently built Relay, Connector, Sidecar, Frontend Bridge, connection SDK, and approved-call HTTP adapter. The parallel Platform production connection hosts are retired; retained Fabric/Relay-runtime V1 support is a compatibility fixture, not a second production deployment. Products publish their own immutable operation catalogs and domain contracts; the Platform policy artifact grants access independently.

Workspace owns the trusted distribution/component catalog and native installer. Client owns the Windows installer and existing Web update controls. Download/stage never changes active releases. Apply requires explicit confirmation of the selected plan and a fresh, atomic maintenance admission check. Missing activity information denies installation. No updater cancels tasks, drains queues, or unloads models automatically.

Product V2 uses owner/operation identifiers and original JSON bytes. The selected server catalog fixes routes and schemas; requests never select URLs, credentials, roles, or catalog versions. Initial migration preserves the current thirteen operations and their authorization/scope behavior. First V2 activation is a coordinated release-group switch; incompatible versions fail preflight without a V1 fallback.

## Implementation ownership

| Group | Ownership |
| --- | --- |
| Dependency split | Plugins connection SDK/runtime; Platform Authority, generic contracts and control-plane/storage boundaries |
| Product V2 contracts | Platform proto mirrors, generic catalog/policy contract, bundle producer and conformance |
| Product adapter / BFF | Plugins approved-call HTTP adapter and Connector; Platform generic BFF and Client request consumer |
| Product owners | Six owner catalogs and actual task-admission/activity integrations |
| Runtime maintenance | Durable admission gate, source authentication, Kernel/agent hooks and broker |
| Native / Windows updates | Workspace bundles/native payloads and Client OCI payloads; common check/stage/apply protocol |
| Artifact publication | Per-component Plugins releases, other owner artifacts, signed catalog metadata, image digests and verified workflow provenance |
| Native identity | Native mTLS, persistent issuance/ACK/registry, signed revocation and live readiness |

Preserve unrelated Gemini edits in Platform and Yield. Source development uses scoped ownership; use a separate worktree when concurrent or dirty changes overlap.

## Independent releases and catalog activation

The connection components use individual component/channel/source-commit release tags. A component change builds that component; shared SDK or contract changes rebuild their dependents. Compatible artifacts can be installed individually. A wire-contract change still requires a coordinated compatible group and fails preflight for mixed versions.

Installers retain a pinned bootstrap catalog for first use. Later catalog changes arrive as immutable, attested Workspace metadata releases and activate only through explicit catalog import. The verifier code and signer identity stay pinned independently of catalog data. Both installers retain a protected generation/digest floor, reject rollback or changed bytes at the same generation, and bind staged update plans to the active catalog. Missing or corrupt initialized metadata must not fall back to bootstrap.

This source migration does not complete signed publication or deployment acceptance. The source checks and delivery records for the follow-up must be recorded separately; the open host-root, Windows and two-host checks below remain open.

## Acceptance checks

- [x] CCV2-DEP: SDK/Sidecar normal dependency closure excludes database, WebAuthn and Platform server implementation packages. Sidecar retains the Tonic/HTTP implementation for its own local IPC host.
- [x] CCV2-EXT: A new Product operation is accepted through owner catalog plus approved policy data without modifying Platform core source. Verified by the dynamic-operation integration fixture.
- [x] CCV2-AUTH: Unknown operations, unauthorized calls, catalog corruption, incompatible pins and cross-Workspace/session responses are rejected in executable source tests. Deployed authorization acceptance remains part of CCV2-REMOTE.
- [ ] CCV2-STAGE: Real active or queued workloads permit download/stage while UI, CLI and API reject apply.
- [ ] CCV2-RACE: Explicit apply and a concurrent new task cannot both acquire admission; task completion never triggers automatic installation.
- [ ] CCV2-CORE: Core-runtime apply requires released Worker/Lease/allocation state; idle resource holders require explicit owner cleanup.
- [ ] CCV2-ROLLBACK: Unhealthy candidates and interrupted installation preserve or recover the old verified release and durable admission state.
- [ ] CCV2-LINUX: Native Linux uses real immutable Python/native payloads; ordinary services restart alone and core dependencies restart in order.
- [ ] CCV2-WINDOWS: Docker Desktop performs a real staged, confirmed OCI update and rollback through the authenticated maintenance broker.
- [ ] CCV2-REMOTE: Baijin-Desktop Linux/WSL hosts Web/BFF, Relay, PostgreSQL and restricted issuance/revocation; ruanyun-GTi hosts GPU runtime, Products and Connector. Force actual Cyrene RELAY over the existing tailnet; verify enrollment ACK, authorized dispatch, revocation, reconnect and post-update access.
- [ ] CCV2-DELIVERY: Record per-repository commits, exact-SHA CI, normal integration and canonical remote read-back.

Each evidence record must identify check ID, source commits/artifact digests, environment, command or workflow run, observed result, and remaining limitations. This round creates no Azure resources; tailnet evidence is not public no-port or ACA production-ingress evidence.

The initial source implementation and component-repository integrations are recorded in the execution log; [PR #27](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/27) records the initial Workspace delivery. Follow-up source merges, exact-SHA CI and remote read-back belong in their own delivery records. Immutable component publication remains blocked on settings authorization and the dedicated read credential; real Linux host-root, Windows and two-host checks remain open.

---
<!-- Chinese Translation / 中文翻译 -->

# 连接组件与受控更新 V2

状态：**源码已实现，部署验收待完成**。源码落盘或单元测试通过不能勾选真实部署验收。

当前源码与环境证据见[执行记录](connection-components-v2-evidence.md)。

Platform 保留身份、Directory、授权、设备绑定、撤销、dispatch fence，以及 Authority/BFF 宿主。Plugins 管理独立构建的 Relay、Connector、Sidecar、Frontend Bridge、连接 SDK 和批准调用的 HTTP adapter。Platform 的平行生产连接宿主已退役；保留的 Fabric/Relay-runtime V1 仅供兼容 fixture 使用。Product 发布自己的固定版本操作目录，Platform 策略单独批准权限；新增操作无需修改核心源码。

Workspace 管理发行目录和原生安装器；Client 管理 Windows 安装器和现有 Web 更新入口。下载/暂存不切换版本；安装需要用户确认具体计划，并原子检查任务准入。活动信息缺失即拒绝。更新器不自动取消任务、排空队列或卸载模型。空闲资源占用者需用户显式释放，才能更新核心 runtime。

首轮 v2 按兼容组件组协调切换；请求只提交 owner/operation、原始 JSON、资源 ID 和幂等键，不选择 URL、凭据、角色或目录版本。保留原十三项操作的权限与 Workspace/session 检查，不静默回退 v1。

连接组件按组件、渠道和源提交独立发布；共享 SDK 或契约变化重建其依赖组件。兼容产物可单独更新，协议变化仍需协调兼容组。安装器只在首次使用时采用固定 bootstrap 目录；后续目录由 Workspace 的不可变签名 metadata 发布，经用户显式导入才激活。校验器源码和签发工作流独立固定，目录数据不再随安装器重新编译。两套安装器保留受保护的 generation/digest 下限，拒绝降级或同代换内容，安装计划绑定当前目录；已初始化目录损坏或丢失时不得退回 bootstrap。这些源码改动不替代签名发布与真实部署验收。

CCV2-DEP、EXT 和 AUTH 已完成依赖与可执行源码验收；Sidecar 为自身本机 IPC 保留 Tonic/HTTP 实现。新增操作由动态目录与策略 fixture 验证，权限检查通过源码测试，部署权限仍归双机验收。更新器已完成真实 broker 与 systemd-user 的本机流程，但 GitHub provenance、任务内容和硬件范围的限制见执行记录，不能替代真实 Product、主机 root 或双机验收。真实双机采用本机 Linux/WSL 控制与身份组件、ruanyun-GTi 的 GPU/Product/Connector，通过现有 Tailscale 网络强制走 Cyrene RELAY。本轮不新增 Azure 资源，不把双机结果宣称为公网免端口或 ACA 生产证明。

每项证据记录检查 ID、提交和产物摘要、环境、命令或 workflow run、观察结果与限制。保留 Gemini 的无关修改；逐仓核对正常集成和远端读回后交付。

初轮组件仓的源码集成见执行记录，[PR #27](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/27) 记录初轮 Workspace 交付；后续各轮合并、精确 SHA 的 CI 与远端读回单独记录。不可变组件发布仍需设置授权和专用只读凭据；Linux 主机 root、Windows 与双机部署检查保持未完成。
