# Connection components and controlled updates V2

Status: **SOURCE IMPLEMENTED; DEPLOYMENT ACCEPTANCE PENDING**. Checkboxes below require actual evidence; a source change or unit test alone does not complete a deployment check.

Current source and environment evidence is recorded in [the execution log](connection-components-v2-evidence.md).

## Authority and compatibility

Platform retains identity, Directory, authorization, device binding, revocation, and dispatch fencing. Connector/BFF sources stay in Platform as separately built packages within the complete deployment. Products publish their own immutable operation catalogs and domain contracts; the Platform policy artifact grants access independently.

Workspace owns the trusted distribution/component catalog and native installer. Client owns the Windows installer and existing Web update controls. Download/stage never changes active releases. Apply requires explicit confirmation of the selected plan and a fresh, atomic maintenance admission check. Missing activity information denies installation. No updater cancels tasks, drains queues, or unloads models automatically.

Product V2 uses owner/operation identifiers and original JSON bytes. The selected server catalog fixes routes and schemas; requests never select URLs, credentials, roles, or catalog versions. Initial migration preserves the current thirteen operations and their authorization/scope behavior. First V2 activation is a coordinated release-group switch; incompatible versions fail preflight without a V1 fallback.

## Implementation ownership

| Group | Ownership |
| --- | --- |
| Dependency split | Platform client SDK, Relay runtime, control-plane/storage boundaries, Cargo registry/lock and package governance |
| Product V2 contracts | Platform proto mirrors, generic catalog/policy contract, bundle producer and conformance |
| Product adapter / BFF | Generic Connector dispatch; generic BFF and Client request consumer |
| Product owners | Six owner catalogs and actual task-admission/activity integrations |
| Runtime maintenance | Durable admission gate, source authentication, Kernel/agent hooks and broker |
| Native / Windows updates | Workspace bundles/native payloads and Client OCI payloads; common check/stage/apply protocol |
| Artifact publication | Immutable component artifacts, image digests and verified workflow provenance |
| Native identity | Native mTLS, persistent issuance/ACK/registry, signed revocation and live readiness |

Preserve unrelated Gemini edits in the canonical Platform and Yield worktrees. Source development uses scoped ownership; the Platform integration worktree is separate from those edits.

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

The source implementation and eight component-repository integrations are recorded in the execution log. Final Workspace source delivery is tracked by [PR #27](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/27), including this plan/evidence revision. Its merge and remote read-back cannot be self-referenced inside the same commit; they are added to the external final delivery record. Immutable component publication remains blocked on settings authorization and the dedicated read credential; real Linux host-root, Windows and two-host checks remain open.

---
<!-- Chinese Translation / 中文翻译 -->

# 连接组件与受控更新 V2

状态：**源码已实现，部署验收待完成**。源码落盘或单元测试通过不能勾选真实部署验收。

当前源码与环境证据见[执行记录](connection-components-v2-evidence.md)。

Platform 保留身份、Directory、授权、设备绑定、撤销和 dispatch fence。Connector/BFF 留在 Platform 独立构建，随完整部署运行。Product 发布自己的固定版本操作目录，Platform 策略单独批准权限；新增操作无需修改核心源码。

Workspace 管理发行目录和原生安装器；Client 管理 Windows 安装器和现有 Web 更新入口。下载/暂存不切换版本；安装需要用户确认具体计划，并原子检查任务准入。活动信息缺失即拒绝。更新器不自动取消任务、排空队列或卸载模型。空闲资源占用者需用户显式释放，才能更新核心 runtime。

首轮 v2 按兼容组件组协调切换；请求只提交 owner/operation、原始 JSON、资源 ID 和幂等键，不选择 URL、凭据、角色或目录版本。保留原十三项操作的权限与 Workspace/session 检查，不静默回退 v1。

CCV2-DEP、EXT 和 AUTH 已完成依赖与可执行源码验收；Sidecar 为自身本机 IPC 保留 Tonic/HTTP 实现。新增操作由动态目录与策略 fixture 验证，权限检查通过源码测试，部署权限仍归双机验收。更新器已完成真实 broker 与 systemd-user 的本机流程，但 GitHub provenance、任务内容和硬件范围的限制见执行记录，不能替代真实 Product、主机 root 或双机验收。真实双机采用本机 Linux/WSL 控制与身份组件、ruanyun-GTi 的 GPU/Product/Connector，通过现有 Tailscale 网络强制走 Cyrene RELAY。本轮不新增 Azure 资源，不把双机结果宣称为公网免端口或 ACA 生产证明。

每项证据记录检查 ID、提交和产物摘要、环境、命令或 workflow run、观察结果与限制。保留 Gemini 的无关修改；逐仓核对正常集成和远端读回后交付。

八个组件仓的源码集成见执行记录，Workspace 最终源码交付由 [PR #27](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/27) 跟踪；本提交不能包含自身最终合并 SHA，合并、CI 与远端读回记录在外部最终交付 JSON。不可变组件发布仍需设置授权和专用只读凭据；Linux 主机 root、Windows 与双机部署检查保持未完成。
