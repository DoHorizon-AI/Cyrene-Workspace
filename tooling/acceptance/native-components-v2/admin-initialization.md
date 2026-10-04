# One-time native host administrator initialization

Status: **BLOCKED ON VERIFIED RELEASE INPUTS; NO INSTALL COMMAND GENERATED**.
This runbook describes the single privileged initialization. It is not an
installer and must not be used with guessed signatures, source commits, builder
identities, manifests, or digests.

状态：**等待已验证发行输入；尚未生成安装命令**。本 runbook 说明一次性特权初始化步骤，本身
不是安装器。不得猜填签名、源码提交、builder 身份、manifest 或摘要。

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
   ownership/modes, and service state. Create a root-owned backup outside the
   active component tree. Verify that the backup can be read back and that its
   digests match; never put credentials or task payloads in the shared report.
2. Keep all four manually sourced runtime processes untouched. Their presence,
   `nvidia-smi` telemetry, and empty process scans do not establish a released
   Worker, Lease, or idle GPU. With activity authority unknown, keep native
   apply admission closed and do not switch any active runtime.
3. After a verifier accepts the exact tuple above, use the native admin wrapper
   to render a command bound to that tuple and the verified backup receipt.
   Review the rendered command before execution. The wrapper is not available
   until the offline broker bootstrap and owner-source inputs are wired; stop if
   it cannot render a command. Never construct the privileged command by hand,
   and never resolve a floating tag or latest URL at execution time.
4. Install the pinned stage-only package bytes only after the backup is
   verified. Package configuration may stage immutable Product releases but
   must not activate Products or replace existing active pointers. Initialize
   trusted activity sources only from the exact verified/installed owner
   metadata plus operator-supplied endpoint and credential-file paths. Never
   print credential contents. A fresh catalog may be created only when the
   catalog, token files, and related runtime state are all absent; preserve any
   existing catalog, tokens, state, or active runtime on upgrade.
5. Initialize only the approved system units and restricted maintenance broker
   entry point using the existing supported API. A missing API or any UNKNOWN
   task/Worker/Lease/resource state blocks broker activation and Product/runtime
   activation. Fresh-host broker unavailability must fail new task admission
   closed. On upgrade, preserve the actual existing gate and state; do not label
   them `CLOSED` without evidence. Keep the prior hand-started runtime tree
   untouched.
6. Verify the broker's durable state and unit definitions before enabling
   any candidate runtime. Existing task state stays `UNKNOWN` until the
   Kernel-bound admission probe reports task, Lease, Worker, and resource
   ownership from the authoritative service.
7. During an authorized workload, prove that download/stage remains allowed
   while apply is refused before maintenance admission or active-pointer change.
   Confirmations must bind the component IDs, each artifact digest, and the
   exact immutable plan ID/digest. A task finishing must not trigger apply.
8. Release a Worker, Lease, and allocated resources only through their owning
   service's explicit release operation. Re-read the authoritative state and
   require a fresh `READY` result before any later apply attempt. Never infer
   release from a process exit or GPU telemetry.
9. Test an unhealthy candidate and an interrupted update in a separately named
   systemd test instance with copied component bytes and a disposable state
   root. The controlled drop-in must fail the candidate; verify the exact prior
   active release, manifest digest, and executable bytes are restored while the
   original signed candidate bytes remain unchanged. Keep admission locked until
   durable recovery is complete; do not run this fault test against the old
   live runtime.
10. Verify normal service order, Relay routing through the existing Tailscale
   path, and control-client-to-host protocol compatibility. Preserve the
   existing Cyrene Relay path; no Azure resource or new SSO path is part of this
   initialization.

## Current host-specific blockers

The remote host currently has no formal broker, updater, activity authority, or
managed system unit. Noninteractive sudo is unavailable. Worker/Lease/resource
ownership is unknown, and the first real Yield + LLaMA-Factory one-step run must
use the authoritative Kernel GPU binding. Therefore this artifact currently
contains no `sudo`, `dpkg`, broker-start, unit-enable, check, stage, or apply
command. The release is not yet published, the offline broker bootstrap and
owner-source configuration are not yet joined into the admin wrapper, and the
Kernel readiness RPC returned `Unimplemented`. Root must first verify the real
signed release and finish the supported broker/catalog initialization path;
only then can the wrapper render the exact one-time command for the
administrator to execute.

当前远端主机没有正式 broker、updater、activity authority 或受管 system unit；非交互 sudo 不可用。
Worker/Lease/资源归属未知，首个 Yield + LLaMA-Factory 单步真机任务必须使用权威 Kernel GPU 绑定。
因此本文不含 `sudo`、`dpkg`、启动 broker、enable unit、check、stage 或 apply 命令。root 必须
先核验真实签名 tuple、接通受支持的 broker/catalog 初始化入口，再为管理员生成最终一次性命令。
