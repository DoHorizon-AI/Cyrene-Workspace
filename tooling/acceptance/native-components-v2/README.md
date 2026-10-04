# Native components V2 acceptance

This package prepares a reviewable, real-host acceptance record for Linux
native components. It records exact local source heads, read-only SSH inventory,
artifact and plan digests, task/resource evidence, and separate cold/warm phase
timings. It does not install software or run Product/GPU workloads.

本工具为 Linux 原生组件生成可审查的真机验收记录，覆盖本地源码精确提交、SSH 只读主机清单、
制品与计划摘要、任务/资源证据，以及分开的冷启动/暖启动阶段耗时。它不会安装软件或提交
Product/GPU 工作负载。

## Files

| File | Responsibility |
| --- | --- |
| [`native_acceptance.py`](native_acceptance.py) | Create runs, prepare an isolated user-space directory, collect read-only host facts, append evidence, and render a Markdown receipt. |
| [`admin_initialize.py`](admin_initialize.py) | Execute a root-only, pin-required, stage-only DEB and fresh broker initialization after official release verification. |
| [`admin_packet.py`](admin_packet.py) | Assemble a reviewable administrator packet from an exact source Git bundle and officially verified signed release inputs. |
| [`operator-tools.lock.json`](operator-tools.lock.json) | Pin the official GitHub CLI archive, checksum file, and executable used by the administrator packet. |
| [`kernel_readonly_probe.rs`](kernel_readonly_probe.rs) | Query only Kernel capabilities and update readiness over an already-running Unix socket. |
| [`admin-initialization.md`](admin-initialization.md) | One-time administrator initialization gates; the executable installation command is intentionally pending the final verified signature tuple. |
| [`admin-init-tuple.template.json`](admin-init-tuple.template.json) | Empty receipt shape populated only by successful official release and attestation verification. |

| 文件 | 职责 |
| --- | --- |
| [`native_acceptance.py`](native_acceptance.py) | 创建 run、准备隔离用户目录、采集主机只读事实、追加证据并生成 Markdown 回执。 |
| [`admin_initialize.py`](admin_initialize.py) | 仅在正式发行验证和精确 pin 齐备后，以 root 暂存 DEB 并初始化全新的维护代理。 |
| [`admin_packet.py`](admin_packet.py) | 使用精确源码 Git bundle 和已通过正式验证的签名发行输入，组装可审阅的管理员初始化包。 |
| [`operator-tools.lock.json`](operator-tools.lock.json) | 固定初始化包所用官方 GitHub CLI 的归档、校验文件与可执行文件摘要。 |
| [`kernel_readonly_probe.rs`](kernel_readonly_probe.rs) | 通过已运行 Unix socket 只读查询 Kernel 能力与更新就绪状态。 |
| [`admin-initialization.md`](admin-initialization.md) | 一次性管理员初始化门禁；最终真实签名 tuple 核定前不生成可执行安装命令。 |
| [`admin-init-tuple.template.json`](admin-init-tuple.template.json) | 仅由正式发行及 attestation 验证成功后填充的空白回执结构。 |

## Run sequence

1. Create a private evidence run with `new-run`; pass one `--source NAME=PATH`
   for each repository whose exact local head belongs in the receipt.
2. Run `prepare-target` only when an isolated target directory is needed. It
   creates `~/.local/share/cyrene/native-operator-acceptance/20261004` with
   mode `0700`, refuses symlinks or unsafe ownership, and never replaces an
   existing directory.
3. Run `collect-target` to capture read-only OS, existing-process, unit-file,
   privilege, Docker-socket accessibility, and NVIDIA telemetry. This inventory
   does not establish task, Worker, Lease, or GPU-idle state.
4. Build the standalone probe with `compile-kernel-probe` from the current
   Platform checkout, then use `query-kernel-readiness` only against the observed
   Kernel UDS. It calls `GetKernelCapabilities` and `GetUpdateReadiness` only.
   An unavailable or unimplemented readiness RPC leaves ownership `UNKNOWN`;
   even zero counters do not open apply or prove GPU idleness. The locally built
   query client is not a signed release artifact or a per-request GPU binding proof.
5. Run `verify-release` only with downloaded immutable release assets and exact
   Workspace ref and commit pins. It calls the canonical verifier with detached
   attestations enabled and requires the release's verified stage-only DEB proof.
   A caller-supplied receipt or `accepted:true` field is never accepted.
6. Use `record-phase` and `record-observation` only with captured evidence.
   Every phase begins with cold and warm states `NOT_RUN`. Fixture/simulated
   evidence cannot mark a phase `PASS`.
7. At the end of the closeout, run `record-latest-source-heads` to capture the
   integration checkout without replacing the original source snapshot used to
   build the probe. Repeat `--source NAME=PATH` for any repository that was not
   in the original run. Then render `report`; inspect the source and digest fields.

1. 使用 `new-run` 创建私有证据 run；每个纳入回执的仓库用一个 `--source NAME=PATH`。
2. 仅在确需隔离目录时运行 `prepare-target`。它会创建
   `~/.local/share/cyrene/native-operator-acceptance/20261004`（权限 `0700`），拒绝符号链接
   或不安全属主，也不会覆盖已有目录。
3. 使用 `collect-target` 只读采集 OS、现存进程、unit 文件、权限、Docker socket 可访问性与
   NVIDIA 遥测。该 inventory 不能证明任务、Worker、Lease 或 GPU 空闲状态。
4. 用当前 Platform checkout 通过 `compile-kernel-probe` 编译独立只读工具，再用
   `query-kernel-readiness` 查询已观测到的 Kernel UDS。工具只调用 `GetKernelCapabilities`
   和 `GetUpdateReadiness`；readiness RPC 不可用或未实现时，所有权继续为 `UNKNOWN`。即使
   计数为零也不会开放 apply 或证明 GPU 空闲。本地编译工具不是签名发行制品，也不证明每请求 GPU 绑定。
5. 只有下载的不可变发行资产及精确 Workspace ref/commit pin 才可运行 `verify-release`。
   它调用正式 verifier 且强制验证 detached attestation，并要求发行 receipt 中的 stage-only DEB
   证明。不得接受调用者提供的 receipt 或 `accepted:true` 字段。
6. 仅凭采集到的证据使用 `record-phase` 和 `record-observation`。所有阶段的冷/暖状态初始
   均为 `NOT_RUN`；fixture/模拟证据不能将阶段记为 `PASS`。
7. 收尾时运行 `record-latest-source-heads` 记录当前集成检出状态，但不覆盖构建查询器时的原始
   源码快照。原 run 未包含的仓库可重复传入 `--source NAME=PATH`；再运行 `report` 生成回执，
   并复核源码和摘要字段。

Example for local preparation and inventory:

```bash
python3 tooling/acceptance/native-components-v2/native_acceptance.py new-run \
  --output /tmp/native-components-v2-run \
  --source Workspace=/path/to/Cyrene-Workspace \
  --source Platform=/path/to/Cyrene-Platform \
  --source Yield=/path/to/Cyrene-Yield

python3 tooling/acceptance/native-components-v2/native_acceptance.py prepare-target \
  --run /tmp/native-components-v2-run/run.json \
  --ssh-alias RuanYun-3090-Tailscale

python3 tooling/acceptance/native-components-v2/native_acceptance.py collect-target \
  --run /tmp/native-components-v2-run/run.json \
  --ssh-alias RuanYun-3090-Tailscale

python3 tooling/acceptance/native-components-v2/native_acceptance.py compile-kernel-probe \
  --run /tmp/native-components-v2-run/run.json \
  --platform-source /path/to/Cyrene-Platform \
  --target-directory /tmp/native-components-v2-kernel-target \
  --output /tmp/native-components-v2-kernel-probe

python3 tooling/acceptance/native-components-v2/native_acceptance.py query-kernel-readiness \
  --run /tmp/native-components-v2-run/run.json \
  --ssh-alias RuanYun-3090-Tailscale \
  --socket /absolute/path/to/kernel.sock

python3 tooling/acceptance/native-components-v2/native_acceptance.py record-latest-source-heads \
  --run /tmp/native-components-v2-run/run.json \
  --source Yield=/path/to/Cyrene-Yield
```

After the final signed release exists, use `verify-release` with the downloaded
immutable assets directory and exact source ref/commit. It derives the target
profile from the captured host inventory and invokes the official verifier with
detached attestations enabled.

The separate `admin_initialize.py` entry point is available for a later,
explicit root invocation. It requires the exact release directory, source ref
and full commit, all five immutable broker bootstrap inputs, and a channel.
It re-verifies the release, checks the package's stage-only proof, backs up and
reads back existing configuration and runtime pointers, installs only the
stage-only DEB, and initializes a broker only through the existing two-step
plan-digest API. It derives owner IDs and credential paths from the signed DEB
and matching installed units, creates activity sources only on a fresh host,
and can start only the new broker when `--start-broker` is explicitly supplied.
It never starts, enables, stops, or switches Product/Core services. It leaves
readiness `UNKNOWN`, apply admission `CLOSED`, and the Platform READY manifest
unwritten when authoritative task ownership is unavailable. Do not execute it
until genuine immutable assets and exact source pins have been verified and a
complete command has been generated from those receipts.

单独的 `admin_initialize.py` 入口可供后续明确的 root 执行。它要求精确发行目录、源码 ref 与完整
commit、五个不可变 broker 引导输入及 channel。它会重新验证发行，核对 package 的 stage-only
证明，备份并回读现有配置与 runtime 指针，只安装 stage-only DEB，并仅通过既有两阶段 plan-digest
API 初始化 broker。owner ID 与凭据路径来自签名 DEB 及字节相同的已安装 unit；activity sources
仅在全新主机创建。只有显式提供 `--start-broker` 才会启动新 broker。它不会启动、enable、停止或
切换 Product/Core 服务。权威任务归属不可用时，readiness 保持 `UNKNOWN`、apply 准入保持 `CLOSED`，
且不写入 Platform READY manifest。只有真实不可变资产与精确源码 pin 经验证并由回执生成完整命令后，
才能执行此入口。

The read-only evidence commands do not check, stage, apply, install, restart,
or submit a workload. The separately documented administrator entry point is
not part of that read-only sequence.
The report summarizes recorded artifact, plan, task, and resource digests, marks
unrun cold/warm phases, and lists the minimum administrator handoff. It does not
render an initialization command until the final signed tuple and supported
offline broker bootstrap path are available.

这些命令不会 check、stage、apply、安装、重启或提交工作负载。报告会汇总已记录的制品、计划、
任务和资源摘要，标明未运行的冷/暖阶段，并列出管理员最终所需的最小步骤。签名 tuple 和受支持
的离线 broker 引导入口核定前，不会生成初始化命令。

以上命令不会 check、stage、apply、安装、重启或提交工作负载。Yield + LLaMA-Factory 单步 CUDA
闭环仍需等待 Kernel 绑定感知 GPU 预检和可信源码/制品 tuple 核验。
