# Acceptance tooling

This directory contains scoped runners for real product and host acceptance.
Each runner owns its evidence format and documents whether it can make changes.

本目录存放真实产品与主机验收的定向工具。每个 runner 独立管理证据格式，并说明是否会修改
环境。

| Directory | Responsibility |
| --- | --- |
| [`native-components-v2/`](native-components-v2/README.md) | Linux native component and GPU acceptance preparation. |

| 目录 | 职责 |
| --- | --- |
| [`native-components-v2/`](native-components-v2/README.md) | Linux 原生组件与 GPU 验收准备。 |

Start with the child directory's README; do not infer acceptance from source
tests, GPU telemetry, or a process scan.

先阅读子目录 README；不得仅凭源码测试、GPU 遥测或进程扫描推断验收通过。
