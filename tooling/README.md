# Tooling

This directory contains operator and developer tools that are not part of the
installed product runtime.

本目录存放运维与开发辅助工具，不属于已安装的产品运行时。

| Directory | Responsibility |
| --- | --- |
| [`acceptance/`](acceptance/README.md) | Real-environment acceptance tools and evidence runners. |

| 目录 | 职责 |
| --- | --- |
| [`acceptance/`](acceptance/README.md) | 真实环境验收工具与证据记录器。 |

Read the directory README before running any tool. Tools must state which host
actions they perform and preserve a clear boundary between observation and
runtime mutation.

运行工具前先阅读对应目录 README。工具必须说明会执行哪些主机操作，并清楚区分只读观测与
运行时变更。
