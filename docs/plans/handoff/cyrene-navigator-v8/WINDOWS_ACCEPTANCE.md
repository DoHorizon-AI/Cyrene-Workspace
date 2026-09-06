# Navigator V8 Windows acceptance / Windows 验收

The requested Windows close-out scenarios passed on the same V8 NSIS package.
This is a private Alpha handoff, with separately operated backends. Exact source,
branch, dependency and binary identities are recorded in `release-lock.json`
and `SOURCE_VERSIONS.md` in the delivery archive.

本次要求的 Windows 收尾场景已在同一份 V8 NSIS 包上通过。交付对象是私有 Alpha 客户端，
后端独立运行；交付压缩包内的 `release-lock.json` 和 `SOURCE_VERSIONS.md` 固定完整源码
SHA、分支状态、依赖与二进制身份。

| Item / 项目 | Exact identity / 精确身份 |
| --- | --- |
| Navigator source | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` |
| V8 NSIS SHA-256 | `792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90` |
| Installed executable SHA-256 | `0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1` |
| DeepSeek Harness | `dsh-v0.1.3-alpha.1` / `d347e703908d0406b7a7ef80e3a0e594d86b2215` |
| Upstream Core patches | `0` |
| Environment | Isolated Windows 11 guest; no Node, Rust or Python development environment, no system VC runtime installation / 隔离 Windows 11 Guest，无 Node、Rust、Python 开发环境，无额外系统 VC runtime 安装 |

## Accepted scenarios / 已通过场景

- [x] **Normal close / 正常退出.** A queued `WM_CLOSE` targeted only the visible
  Tauri main window, through its normal close handler. The internal Tao message
  window was excluded. Both closes removed all 9 owned processes and the
  original Harness/CDP listeners, in 925 ms and 868 ms. No process kill or fault
  cleanup was used to obtain either PASS.
- [x] **Persistence outage and recovery / 持久化服务中断与恢复.** The actual
  persistence process was faulted and restarted against the same database.
  During the outage, the Session stayed at 68 events, 4 Tool records and 54
  Exchange requests. Service restart preserved the complete event prefix.
- [x] **Explicit takeover / 显式接管.** The guest explicitly took writer epoch
  3→4, then 4→5 and 5→6 after desktop restarts. The former host writer's GUI Send
  was rejected; its attempted send left the server history and request IDs
  unchanged. No automatic takeover occurred.
- [x] **Input recovery / 输入恢复.** An unconfirmed input survived the outage
  and normal desktop close. The visible **Restore input** action restored the
  original text without retyping. History stayed unchanged until an explicit
  **Send**. A durable receipt then cleared the retained local copy: 68→77 events,
  54→55 Exchange requests, still 4 Tool records.
- [x] **No historical Tool execution / 历史 Tool 不重放.** Reading, restarting,
  taking over and restoring input did not execute historical Tool records or
  issue additional model requests. Only an explicit new Tool turn added a Tool
  record.

两次正常退出只进入可见主窗口的关闭路径，未使用进程终止代替通过。持久化服务的强制中断
属于独立的服务故障注入，不是桌面正常退出的验收手段。旧 V7 错误遍历内部窗口及其强制
清理记录仍作为历史失败保留，没有计入上述 PASS。

The full staged and installed guest runtime trees match: **24,382 files**, no
missing, additional or changed files. Their stable content map SHA-256 is
`1b2fd08a483fc050e6cd0e6cdfd477220d39f06f10f0b085899ec9505fe096a3`.
The archive includes this complete map; the check is not a selected-file sample.

完整 staging 与 Guest 安装后的 runtime 共 **24,382 个文件**，无缺失、额外或变更文件；
稳定内容清单的 SHA-256 如上，清单随包交付，本检查覆盖完整目录。

## Frozen backend confirmation / 冻结后端复验

After the recovery scenarios, Exchange was restarted from the exact frozen
Exchange, Platform and Plugins commits listed in the release lock. Persistence
remained at Navigator `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe`. The Windows
package was unchanged. This final combination completed one fresh GUI
model → Rust Tool → model continuation turn: 77→90 events, 4→5 Tool records,
55→57 Exchange requests, 153 received WebSocket frames and final provider Usage
on both new requests. All 77 earlier events remained identical.

恢复场景完成后，Exchange 以 release lock 中的 Exchange、Platform、Plugins 固定提交重新
启动；Persistence 保持上述 Navigator 版本，Windows 安装包未改变。最终组合完成了一次新的
GUI 模型→Rust Tool→模型续答：77→90 events、4→5 Tool records、55→57 Exchange requests，
接收 153 个 WebSocket frames，两条新请求均有最终 provider Usage，原 77 个事件完整保留。
恢复场景的早期 Exchange/CES 启动记录作为兼容性历史保留，未被改写成最终源码运行记录。

The handoff connector was separately exercised with the installed V8 executable
under Windows PowerShell 5.1: **9 passed, 0 failed, 0 skipped**. These are
`-ValidateOnly` configuration and URL-policy checks; they do not claim that a
new colleague's network or credentials have been provisioned.

连接脚本另经 Windows PowerShell 5.1 和实际安装的 V8 程序验证：**9 passed，0 failed，0
skipped**。这些是 `-ValidateOnly` 的配置与 URL 策略检查，不表示新同事的服务地址或凭据
已经开通。

## Limits / 限制

- The client is unsigned and the accepted installation path is the per-user
  NSIS default. The MSI build hash is retained in the version record, but MSI
  installation is not the handoff's accepted path.
- The guest is a second client OS on the same physical host. This does not
  establish two physical GPU nodes, Workspace enrollment, SSO or enterprise RBAC.
- Exchange and persistence must already be deployed and reachable. The client
  installer does not deploy them, a model server or a GPU node.
- The wider P0-29 upgrade-failure diagnostic remains unverified on V8. Hosted
  CI is blocked by billing/spending limits; these local results do not claim a
  green remote run, a merge or the full Phase 0 gate.
- Phase 1–4 training, Reactor deployment, feedback and enterprise acceptance
  remain separate work.

客户端未签名，验收采用 NSIS 默认的按用户安装路径。隔离 Guest 不代表两台物理 GPU。
Exchange 与 Persistence 需要预先部署并可达。V8 的真实升级失败诊断、受账单限制阻塞的
hosted CI、主线合并和 Phase 0 总门仍分别记录状态；本次收尾不提前通过 Phase 1–4。

The archive includes sanitized acceptance summaries and hashes of the private
receipts. It excludes credentials, ready URLs, local databases and raw Session
contents. `SHA256SUMS` covers every shipped file except itself.

交付包只携带脱敏验收摘要与私有原始收据的哈希，不携带凭据、ready URL、本地数据库或完整
Session 内容。`SHA256SUMS` 覆盖除自身以外的每个交付文件。
