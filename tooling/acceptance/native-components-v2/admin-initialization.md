# One-time native host administrator initialization

Status at **2026-10-04 13:30 UTC**: **BLOCKED; NO VERIFIED ADMINISTRATOR
PACKET; NO INSTALL COMMAND GENERATED**. At this checkpoint the native installer release
was unpublished; staging was blocked in Actions run `37205539712`. This runbook
describes the privileged bootstrap boundary; it is not an installer and must
not be used with guessed signatures, source commits, builder identities,
manifests, or digests. A later verified packet receipt supersedes this dated
status.

截至 **2026-10-04 13:30 UTC**：**受阻；尚无已验证管理员 packet 或安装命令**。当时 native
installer 发行尚未发布，staging 受阻于 Actions run `37205539712`。本文说明特权引导边界，本身
不是安装器。不得猜填签名、源码提交、builder 身份、manifest 或摘要。后续已验证 packet receipt
将取代此日期状态。

## Inputs that must be pinned first

Do not hand-fill [`admin-init-tuple.template.json`](admin-init-tuple.template.json)
or copy values from an earlier report. After the immutable release is published,
run the acceptance runner's `verify-release` command with the exact
Workspace ref and 40-character source commit. It selects the Ubuntu target from
the current read-only host inventory and calls
`scripts/native_installer_release.py verify` with attestation verification
enabled. The verifier must check:

- The fixed repository `DoHorizon-AI/Cyrene-Workspace`, workflow
  `DoHorizon-AI/Cyrene-Workspace/.github/workflows/native-installer-release.yml`,
  exact source branch/ref and commit, immutable release ID, and workflow run.
- The signed release manifest, source receipt, `SHA256SUMS`, Python runtime
  lock, both DEB assets, and their detached GitHub attestations.
- The selected DEB's embedded stage-only marker, original and staged service
  index bytes, all five Product artifact/manifest/attestation tuples, and the
  actual `postinst`, `prerm`, and `postrm` script hashes, plus the locked private
  CPython and `uv` executable bytes, paths, hashes, and modes.
- The verifier's checks that service activation is deferred, broker state and
  old runtime are preserved, maintainer scripts are stage-only, and the
  original signed Product bytes remain present in the DEB.

The runner imports the canonical release helper and forces detached attestation
verification; there is no `--skip-attestations` path. The release helper checks
the expected repository, workflow, source ref, source commit, attestation
predicate, subject name, and digest. The populated receipt is derived only
after those checks succeed. It does not trust a caller-supplied `accepted:true`
field or claim an OIDC identity that the verifier did not return. A changed DEB
or source-receipt byte is rejected after verification.

The template stays empty in this checkout because no final signed native
installer release is available yet. No installer command is generated from an
empty, partial, or operator-authored tuple.

## One-time execution order

1. Record the host's current component roots, unit files, active pointers,
   ownership/modes, and service state. The privileged initializer creates a
   root-owned backup outside the active component tree, reads it back, and
   records its path and digest in the root-owned recovery journal before
   installing the DEB. Its final redacted receipt reports the backup evidence.
   Preserve that receipt; never put credentials or task payloads in the shared
   report.
2. Keep all four manually sourced runtime processes untouched. Their presence,
   `nvidia-smi` telemetry, and empty process scans do not establish a released
   Worker, Lease, or idle GPU. With activity authority unknown, keep native
   apply admission closed and do not switch any active runtime.
3. Do not execute an administrator command until the signed release verifier
   and packet assembler have produced a complete packet from the exact immutable
   inputs above. The packet binds the verified release and bootstrap input bytes;
   it does not need a host backup receipt. The root-run initializer creates and
   verifies that backup before installing the DEB, then returns its receipt.
   Review the generated packet; never construct a privileged command by hand or
   resolve a floating tag or latest URL at execution time. No valid packet or
   command existed at the dated checkpoint above.
4. The supported host path is deliberately stage-only. After the packet exists
   and the backup is verified, `admin_initialize.py` re-verifies the release,
   installs only the signed DEB bytes, and bootstraps the maintenance broker
   through its two-step exact-plan / explicit-plan-digest confirmation API. The
   broker bootstrap is fresh-host only; the initializer preserves existing
   broker state on upgrade. It derives the five Product activity-source IDs and
   UID/GID owners from the signed Product units and requires matching installed
   unit bytes. Operator endpoint and credential-file paths remain explicit
   inputs; never print credential contents. Activity catalog and token metadata
   may be created only when their related state is absent. Existing catalog,
   tokens, state, and active runtime must be preserved.
5. `--start-broker` is optional and starts only the newly initialized broker
   after explicit plan confirmation. Product and Core services are not started,
   enabled, stopped, or switched; no active pointer is changed. The initial
   state remains `UNKNOWN` for task/Worker/Lease/resource ownership and apply
   admission remains `CLOSED`. No task is automatically cancelled or unloaded.
   If a fresh broker cannot enforce fail-closed admission, stop before starting
   it. Do not relabel an existing upgrade gate `CLOSED` without evidence.
6. Treat this host bootstrap as completion of package staging only. Do not
   enable a candidate runtime or report readiness until a Kernel-bound probe
   reads authoritative task, Lease, Worker, and resource ownership and returns
   a fresh `READY` result. Process exit and GPU telemetry do not prove release.
7. After authoritative readiness,
   verify that download/stage remains allowed while apply is refused before
   maintenance admission or active-pointer change. Confirmations must bind the
   component IDs, each artifact digest, and exact immutable plan ID/digest. A
   task finishing must not trigger apply. Release a Worker, Lease, and allocated
   resources only through their owning service's explicit release operation.
8. Test an unhealthy candidate and interrupted update in a separately named
   systemd test instance with copied component bytes and disposable state. The
   controlled drop-in must fail the candidate; verify that the exact prior
   active release, manifest digest, and executable bytes are restored while the
   original signed candidate bytes remain unchanged. Keep admission locked until
   durable recovery is complete; never run this fault test against the old live
   runtime.
9. Treat control-host initialization as a separate, uncompleted workstream.
   `control_initialize.py` is read-only `PLAN_ONLY`: it provisions no local
   Authority, Relay, BFF, database, service, or caller identity. Provisioning
   still needs reviewed PostgreSQL roles and six component migration URL files,
   the Authority signing key (32 bytes) and key ID, Authority TLS material and
   trust configuration, the device-CA runtime URL and its issuer private key,
   certificate, and issuer ID, the reviewed initial artifact selector, and the
   real Entra tenant UUID and audience. The control host is Ubuntu 24 and the
   target is Ubuntu 22; retain the existing Relay over Tailscale. The
   `AzureAdWebPrincipalVerifier` implements delegated `Workspace.Web.Access`
   user-JWT verification, but this setup has no supplied real tenant/audience
   configuration or user JWT acceptance evidence. The alternative Google and
   local OIDC paths are not implemented. Health mTLS remains a separate
   workload identity. Do not claim control-plane or caller-token acceptance
   from a successful read-only plan.

## Current native and control-host blockers

At the **2026-10-04 13:30 UTC** checkpoint, the release was unpublished and
its staging gate was blocked in Actions run `37205539712`; no verified
administrator packet or command was available. That is historical status: a
later verified packet receipt supersedes it. This artifact contains no
`sudo`, `dpkg`, broker-start, unit-enable, check, stage, or apply command. The
initializer already implements stage-only signed-DEB installation, exact-plan
broker bootstrap, and activity source owner derivation from the five signed
Product units; do not describe those paths as unwired. Even after host
bootstrap, activity ownership remains
`UNKNOWN`, apply remains `CLOSED`, and Core/Product activation is **NOT RUN**
until separate authoritative Kernel readiness and activation evidence exist.

Control-host setup is also incomplete: `control_initialize.py` produces a
read-only `PLAN_ONLY` report and performs no provisioning or migration. Required
inputs include reviewed database roles and six migration URL files, the
Authority signing key (32 bytes) and key ID, Authority TLS material and trust,
the device-CA runtime URL and issuer private key/certificate/ID, selector
configuration, and real Entra tenant/audience values plus a user JWT for
acceptance. `AzureAdWebPrincipalVerifier` implements the delegated
`Workspace.Web.Access` flow; Google and local OIDC alternatives are not
implemented. Health mTLS does not establish this delegated user identity. No
remote install, GPU run, Relay acceptance, or control-plane readiness is
evidenced by this runbook.

截至 **2026-10-04 13:30 UTC**，发行尚未发布，staging gate 受阻于 Actions run `37205539712`；
当时没有已验证管理员 packet 或命令。此状态仅代表该日期；后续已验证 packet receipt 将取代它。
因此本文不含 `sudo`、`dpkg`、启动 broker、enable unit、check、stage 或 apply 命令。现有
initializer 已实现签名 DEB 仅暂存安装、精确 plan 的 broker 两阶段引导，以及从五个签名 Product
unit 推导 activity source owner；不得再称这些入口尚未接通。即使主机引导完成，activity 归属仍为
`UNKNOWN`、apply 仍为 `CLOSED`，Core/Product 激活仍为 **NOT RUN**，直到取得独立的 Kernel 权威
readiness 与激活证据。

控制主机初始化也未完成：`control_initialize.py` 只生成只读 `PLAN_ONLY` 报告，不执行 provisioning
或 migration。后续仍需经审查的数据库角色和六个 migration URL 文件、Authority 32 字节签名密钥及
key ID、Authority TLS/trust、device-CA runtime URL 与 issuer 私钥/证书/ID、selector，以及真实 Entra
tenant/audience 和用于验收的用户 JWT。`AzureAdWebPrincipalVerifier` 已实现 delegated
`Workspace.Web.Access` 验证；未实现的是 Google 和本地 OIDC 替代路径。health mTLS 不等同于 delegated
用户身份。本文不证明已执行远端安装、GPU 任务、Relay 验收或控制面 readiness。
