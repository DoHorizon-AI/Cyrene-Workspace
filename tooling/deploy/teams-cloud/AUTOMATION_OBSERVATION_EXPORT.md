# Automation host health export / 自动化主机健康导出

`export_automation_observations.py` reads the final report, manifest, and sample file produced by the existing root health observer. It checks a root-only trust profile, exact SHA-256 pins, owner and mode, path scope, the pinned `cyrene-teams-health-manifest-v1` project/service set, observer report bindings, and the freshness of the final JSONL sample. It parses only the final sample row and writes one bounded status file.

The exporter does not start the observer, call Docker, inspect container sockets, make provider requests, or report business readiness. An observer report marked `RUNNING` can contain a valid one-shot sample; the exported state is derived from that exact sample’s checks. `healthy` means the newest sample had the full pinned service set and no recorded health failures. `degraded` and `unknown` remain explicit outcomes.

The trust profile is a root-owned regular file with mode `0600`. It contains these exact fields:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | `1` |
| `project` | The pinned observer project, `cyrene-teams-cloud` |
| `sourceRoot` | Root-owned private directory containing the pinned manifest and observer run files |
| `manifestPath`, `reportPath`, `samplePath` | Exact input files selected by the root operator |
| `expectedManifestSha256`, `expectedReportSha256`, `expectedSampleSha256` | Lowercase SHA-256 pins supplied by the trusted host profile |
| `hostRef` | Opaque, profile-approved host reference written to the status file |
| `serviceAliases` | Selected manifest service names mapped to opaque aliases; only aliases are exported |
| `maxAgeSeconds`, `maxClockSkewSeconds` | Bounded freshness limits |
| `outputDirectory`, `outputName`, `outputGid` | Root-owned group-readable export directory and fixed output name/group |

The profile itself and each source file must be root-owned and mode `0600`. The private source root and its parent directories must not be group or world writable. The export directory must be root-owned mode `0750`; the exporter atomically writes the fixed status file as root-owned mode `0640`. Mount that directory read-only into the DSH health port. The status file contains only `schemaVersion`, `observedAt`, the opaque `hostRef`, aggregate `state`, and approved `{serviceAlias, state, restartCount}` rows. It omits sample paths, report IDs, container IDs, image IDs, ports, networks, and host metrics.

Run one export after the root observer has produced and pinned its current one-shot sample:

```bash
python3 tooling/deploy/teams-cloud/export_automation_observations.py \
  --trust-profile <root-only-trusted-profile>
```

The trust profile is an operator-mounted runtime input; this repository contains no VM address, host identity, source path, or live hash. A missing, stale, changed, or incorrectly scoped input exits with a stable error code and does not replace the previous status file. The consumer must still enforce the status file’s `observedAt` freshness. Local tests use fully synthetic manifests and samples; they do not establish live VM acceptance.

`export_automation_observations.py` maintains the narrow boundary between the private root observer and DSH. Any real VM integration remains gated on the operator-provided trust profile and read-only mount.

---

`export_automation_observations.py` 读取现有 root 健康观察器生成的最终 report、manifest 和 sample 文件。它会校验 root-only trust profile、SHA-256 固定值、文件所有者与权限、路径范围、固定的 `cyrene-teams-health-manifest-v1` 项目与服务集合、观察器回执绑定，以及最后一条 JSONL sample 的新鲜度。解析器只处理最后一条 sample，并写出一个有界状态文件。

导出器不会启动观察器、调用 Docker、访问容器 socket、请求 provider 或报告业务 readiness。标记为 `RUNNING` 的观察器回执仍可能包含有效的一次采样；导出状态只根据该条 sample 的实际检查结果生成。只有最新 sample 包含固定服务集合且无健康失败时，状态才为 `healthy`；`degraded` 和 `unknown` 会明确保留。

trust profile 必须是 root-owned 普通文件，权限为 `0600`。其中包含上表列出的字段：固定项目、私有输入路径、manifest/report/sample 摘要、opaque `hostRef`、经批准的 service alias、刷新时限，以及固定输出目录和组。profile 与源文件均必须由 root 所有且权限为 `0600`；私有源目录及父目录不能对组或其他用户开放写入。导出目录必须为 root-owned `0750`，状态文件以 root-owned `0640` 原子替换。DSH health port 只读挂载这个目录。

输出仅包含 `schemaVersion`、`observedAt`、opaque `hostRef`、聚合 `state` 和批准的 `{serviceAlias, state, restartCount}` 行。它不会携带样本路径、report ID、容器/镜像 ID、端口、网络或主机指标。缺失、过期、变化或范围不符的输入会返回稳定错误码且不替换旧状态文件；读取方仍须检查 `observedAt`。本地测试使用纯合成数据，不构成真实 VM 验收。
