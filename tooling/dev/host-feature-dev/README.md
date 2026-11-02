# Host/feature separation developer stack

`hfdev.py` starts an isolated copy of the Teams runtime on a developer machine and
runs a signed Teams event through Navigator, Exchange, one tool plugin, and back to a
Teams reply. It also records the baseline numbers used by the host/feature separation
plan (container inventory, memory, idle CPU, and Teams cold start under CPU quotas).

`hfdev.py` 在开发机上启动隔离的 Teams 运行时副本，让一条签名 Teams 事件经过
Navigator、Exchange、一个工具插件并回到 Teams 回复，同时记录宿主/功能分离计划使用的基线数据。

## What runs / 运行内容

The stack is the production [`compose.yaml`](../../deploy/teams-cloud/compose.yaml) plus
[`compose.computer-runtime.yaml`](../../deploy/teams-cloud/compose.computer-runtime.yaml)
and the developer overlay [`compose.dev.yaml`](compose.dev.yaml):

| Service | Source | Notes |
| --- | --- | --- |
| postgres, workspace-authority, account-center, exchange, navigator-persistence, navigator, teams-provider, teams-ingress, computer-runtime, computer-runtime-proxy | production definitions | Images built for the host architecture from exact commits. Navigator runs with `CYRENE_CONVERSATION_ASSISTANT_ENABLED=false`. |
| microsoft-fixture | `fixtures/microsoft_fixture.py` | Answers Bot Framework JWKS, Entra token, and Bot Connector paths through private aliases only. |
| model-stub | `fixtures/model_stub.py` | Deterministic OpenAI-compatible SSE model: one `computer.directory.list` call, then a final answer. |
| hfdev-probe | `fixtures/teams_smoke.py` | Smoke driver and readiness probe; excluded from metrics. |

The production Authority Dockerfile cross-compiles for arm64 only, so
[`docker/Dockerfile.workspace-authority`](docker/Dockerfile.workspace-authority) builds the
same binaries natively; a test keeps its runtime stage identical. The executor image's
Antigravity CLI is a labelled placeholder because the stub model is selected.

GitHub Issues is not started: `computer-runtime` is the only `tool.provider.v1` plugin that
works without credentials or internet access.

## Isolation / 隔离

* Every Compose network is `internal: true` with an explicit subnet from `10.249.0.0/16`;
  `init` picks a free `/20` and refuses overlaps.
* No host port is published. `up` and `status` fail if any container publishes a port in
  `18680–18695` or outside `18700–18749`.
* Project name `cyrene-hfdev-<instance>`; every container carries
  `io.cyrene.task=host-feature-split-20261102`. Other projects are never touched.
* State lives under `~/.local/state/cyrene-host-feature-dev/instances/<instance>/`
  (mode 0700). Keys and certificates are generated per instance and are fixtures only.

Host actions: Git fetch of exact commits from the fixed Cyrene remotes; Docker build/pull;
Compose lifecycle for this project; one root helper container that changes ownership of
files inside the instance directory so service UIDs 10001/999 can read them.

## Usage / 用法

```bash
cd Cyrene-Workspace
# Passing H0 path (deployed Navigator + Exchange). Unpinned `run` uses origin/develop
# and currently fails: grant schema (H0-01) and Teams channel task visibility (H0-02).
# 走通路径需钉住线上 Navigator/Exchange；不钉则默认 origin/develop，目前会失败（见 H0-01/H0-02）。
python3 tooling/dev/host-feature-dev/hfdev.py --instance h0 run \
  --pin navigator=aa128de69058f36f8b8ed97fae4a9e7773dea168 \
  --pin exchange=279a1e09efd0fb10096822d57b9de0e574b8f5e9
python3 tooling/dev/host-feature-dev/hfdev.py --instance h0 cpu-compare --cpus 0.05 --cpus 0.5 --repeat 5
python3 tooling/dev/host-feature-dev/hfdev.py --instance h0 status
python3 tooling/dev/host-feature-dev/hfdev.py --instance h0 down --volumes
```

`build --pin exchange=<40-char SHA>` (also `platform`, `plugins`, `navigator`) overrides the
default `develop` head of a repository; `run` accepts the same `--pin` options. The Workspace
source is the committed `HEAD` of this checkout; `tooling/deploy/teams-cloud` must be clean.
Builds use `SOURCE_DATE_EPOCH` and no attestations, so identical sources give identical image
IDs on the same Docker store. Image tags carry the instance name.

`init --work-mode` selects the Navigator Work configuration:

| Mode | Compose files | Use |
| --- | --- | --- |
| `legacy` (default) | base + computer-runtime + dev overlay | Production base configuration: one Navigator service principal. |
| `actor` | adds `compose.automation-workflows.yaml` | Actor-scoped Work tokens (`CYRENE_WORK_ACTOR_TOKEN_MAP_FILE`, `CYRENE_TEAMS_MODEL_ACTOR_SUBJECT`). |

Run several instances side by side with distinct `--instance` names; each gets its own
project, subnet block, keys, and image tags.

| Command | Result |
| --- | --- |
| `build` | `images.lock.json` with source commits, image IDs, and Dockerfile digests. |
| `init` | Fixture identities, keys, certificates, configs, and `compose.env`. |
| `up` | Production cold-start order; exports connector bindings and the live tool catalog from Exchange before Navigator starts; `evidence/*-up/up.json`. |
| `smoke` | Negative cases (unsigned, attacker-signed, wrong `serviceurl`, expired, wrong audience, foreign tenant) must not be queued; the signed message must produce a reply that quotes the digest of a non-error tool result. Each run has a normalized digest; `consistent` requires all runs to match. Exchange and Work ledgers are saved alongside. |
| `measure` | Container count, limits, memory, and idle CPU seconds from cgroup `cpu.stat`. |
| `cpu-compare` | Per quota: provider → fresh Exchange → ingress cold start, readiness, first and warm reply, CPU and throttled seconds. |

All durations use `CLOCK_MONOTONIC`, which containers share with the host: WSL steps the wall
clock backwards under load. Readiness is timed from the recreate command to the probe's first
successful check (ingress `/health/ready`; provider mTLS gRPC Health).

Evidence: `instances/<instance>/evidence/<UTC>-<command>/`. Results that depend on the
Microsoft fixtures or the model stub are `SIMULATED`; the Cyrene service path is real.

## Files / 文件

| File | Responsibility |
| --- | --- |
| `hfdev.py` | CLI: build, init, up, smoke, measure, cpu-compare, status, logs, down, run. |
| `compose.dev.yaml` | Developer overlay on the production Compose files. |
| `docker/Dockerfile.workspace-authority` | Native-architecture Authority build. |
| `fixtures/microsoft_fixture.py` | Microsoft endpoint fixture. |
| `fixtures/model_stub.py` | Deterministic model stub. |
| `fixtures/teams_smoke.py` | Signed activity driver and readiness probe. |
| `tests/test_hfdev.py` | Offline guard, parity, and fixture tests. |

Offline checks: `uvx --from pytest==9.1.1 pytest -q tooling/dev/host-feature-dev/tests`.
