# Connection components V2: execution evidence

Status: **IMPLEMENTING**. This log distinguishes source delivery, local checks, and deployment acceptance. Refer to [the implementation plan](connection-components-v2.md) for the acceptance checks.

## Immutable owner catalogs

The following commits contain only the V2 operation catalog and its contract index. Their Git objects were read back locally on 2026-10-01 and pushed to each repository's `feat/product-workspace-v2` branch to start the existing source CI and expose immutable bundle inputs. They have not been integrated into `develop` yet.

| Repository | Catalog source commit | Hosted CI / integration |
| --- | --- | --- |
| Catalyst | `75ad5ded966f66609f2b016fb96c62be1511ef02` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Catalyst/actions/runs/36927346542); [draft PR #14](https://github.com/DoHorizon-AI/Cyrene-Catalyst/pull/14), unmerged |
| Yield | `4fc49155786e594ec5f67e5c8f42ec5b35cdd9ab` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Yield/actions/runs/36927350192); [draft PR #17](https://github.com/DoHorizon-AI/Cyrene-Yield/pull/17), unmerged |
| Reactor | `c8f11c09b4f873bf1a550e83a528c5ea05c0b446` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Reactor/actions/runs/36927355529); [draft PR #15](https://github.com/DoHorizon-AI/Cyrene-Reactor/pull/15), unmerged |
| Exchange | `8a6258e656d90bc8fcc22ce570b61917fad7763a` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Exchange/actions/runs/36927360468); [draft PR #20](https://github.com/DoHorizon-AI/Cyrene-Exchange/pull/20), unmerged |
| Echo | `4de40bd9e50f3048491b00e868f7767911237b63` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Echo/actions/runs/36927363424); [draft PR #14](https://github.com/DoHorizon-AI/Cyrene-Echo/pull/14), unmerged |
| Navigator | `8224a47551533c1d17857d418ff63dc1857b8b2d` | [Catalog CI passed](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/36927832527); [draft PR #11](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/11), unmerged |

The catalog worker reports that all six catalogs pass Draft 2020-12 validation, the existing thirteen-operation projection comparison, unique OpenAPI route resolution, schema-pointer resolution, route-parameter coverage, and scope-binding checks. These catalog checks are static evidence. The executable contract and authorization checks below provide additional source evidence; deployed dispatch remains pending. Navigator append remains catalogued without a Platform policy grant.

## Trusted update metadata

Workspace commit `1231c7843547458e10743950c5d71dea733c15e7` contains the trusted component catalog and manifest/index schemas. The catalog raw SHA-256 is `7bde95f6f274dd817bec50af8722931049931e260cb036d3f89c19f5fde6560c`. [Exact-commit CI passed](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/36928866918). The native updater and single-service builder are separate, uncommitted work; this metadata commit does not prove their publication or runtime behavior.

## Local source checks

These results apply to the integration worktree and are not yet associated with final delivery commits:

| Area | Observed result | Limit |
| --- | --- | --- |
| Generic contracts | Locked offline Cargo: 4 unit and 4 dynamic-operation integration tests passed; the real six-owner bundle smoke passed; Python bundle suite: 12 passed. | Final Cargo rerun required after the last API export and strict JSON parser changes. |
| Control plane | Locked offline Cargo: 53 passed. Unknown/unauthorized operations and scope/pin enforcement are covered. | Connector composition and deployed dispatch remain pending. |
| HTTP adapters | Locked offline Cargo: 14 passed, including route binding, payload limits and slow-response timeout. | No remote Product request has been accepted yet. |
| Client V2 | Node 24.21.0: 13 Vitest tests, full TypeScript check and Web build passed. | Latest updater/UI changes need their final checks. |
| Product activity hooks | Product worker reports 81 targeted tests passed across the six repositories, plus scoped lint/format checks. Six Dockerfiles passed Buildx configuration checks. | No image build or broker-backed admission run is included in this result. |

The Product lock references all six catalog commits above and pins the policy and bundle digests. Navigator append has no approved policy grant. Remote connection, real user approval, update races and crash recovery must be recorded separately with their actual execution evidence.

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
| CCV2-DELIVERY | Catalog commits were pushed to task branches; the Platform integration worktree preserves the canonical Gemini edits. | Remaining implementation commits, exact-SHA CI, normal integration and canonical remote read-back. |

No SSH keys have been moved, and no Azure resources have been created. Remote and Windows deployment checks remain **NOT RUN**. Continue local implementation and acceptance while access is being prepared.

---
<!-- Chinese Translation / 中文翻译 -->

# 连接组件 V2：执行证据

状态：**实施中**。本记录分别说明源码、静态检查与真实部署验收；验收条目见[实施计划](connection-components-v2.md)。

六个 Product 已分别提交固定版本的 v2 操作目录和契约索引，具体 SHA 见上表。主线程已读回这些 Git 对象，并推送到各仓 `feat/product-workspace-v2` 工作分支，启动现有源码 CI；尚未集成到 `develop`。上述目录提交的精确 SHA 源码 CI 均已通过，草稿 PR 已创建但尚未合并；后续任务门禁和发布修改仍待提交与验证。目录负责代理报告六仓静态 schema、十三项操作对照、OpenAPI 路由唯一性、schema 指针、参数覆盖及作用域绑定检查通过。本机通用契约动态操作测试、控制面 53 项和 adapter 14 项测试已通过；它们提供源码验证，尚不代表部署后的真实调用验收。Navigator append 没有 Platform 策略批准，继续默认拒绝。

Workspace 的受信更新目录与三份 schema 已单独提交，精确提交 CI 通过；安装器和单服务 builder 尚在实现，不能把目录提交当作其发布证据。Client v2 在 Node 24 下通过 13 项测试、TypeScript 检查和 Web 构建。六个 Product 的活动门禁定向测试共 81 项通过，Dockerfile 仅完成配置检查；真实镜像与 broker 验收仍待执行。

本机已建立独立 PostgreSQL 17 验收容器，2026-10-01 20:58 UTC 的 `pg_isready` 返回可接受连接。它只绑定本机 loopback，使用独立持久卷，密钥配置置于组件版本目录之外；旧数据库未改动。真实 `psql` 连接以 `verify-full` 验证主机身份，连接使用 `TLSv1.3`，错误主机名被拒绝。配置参照 PostgreSQL 17 官方服务端 TLS 与客户端验证文档。签发、审批、证书 ACK、持久 registry 和撤销仍待验收，应用必须使用受限数据库角色。

远端主机可经 tailnet 访问，SSH 主机密钥校验通过，但登录缺少可用密钥。用户正在准备迁移密钥，双机验收保持未执行。当前 WSL 能使用 Docker，Windows Docker Desktop 上下文不可用，Windows 真实安装及回滚也保持未执行。继续完成本机实现与验收，不把待执行项目计为通过。

尚未迁移 SSH 密钥，未创建 Azure 资源。最终交付仍需逐仓提交、精确 SHA 的 CI、正常集成和远端读回。
