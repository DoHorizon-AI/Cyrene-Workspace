# Google Antigravity ACP third-party notice

The opt-in Antigravity ACP image build downloads Google's proprietary standalone ACP server, version 1.4.5. The executable is not committed to this repository. The Dockerfile fetches it only from Google's `dl.google.com` release host and verifies the architecture-specific SHA-256 before placing it in the image.

The ACP registry entry names `./agy_acp_server.par` and the fixed Linux argument `--uid=`. Navigator owns that argument when launching the executable; Compose exposes no caller-controlled argument or shell string. Both supported Linux archives contain `agy_acp_server.par` and its executable sibling `localharness_external`; the image installs both side by side with mode 0555 so relative helper resolution remains available. The uncompressed helper is 133,926,136 bytes on x86_64 and 127,512,832 bytes on ARM64.

- Google Antigravity ACP registry entry: <https://raw.githubusercontent.com/agentclientprotocol/registry/refs/heads/main/antigravity-acp/agent.json>
- Google x86_64 archive: <https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.4.5-linux-x86_64.zip>
- Google ARM64 archive: <https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.4.5-linux-arm64.zip>
- x86_64 archive SHA-256: `923923a0d7dc8073f66cc6f78647a3107e3a7ee2b7e1a24df8b3927b5188013b`
- ARM64 archive SHA-256: `7d64246633ae094f67e3bbcd2d679e89048f91346d5339313336e7483f40f056`

The resulting image contains the third-party executable and must be used and distributed according to Google's applicable terms. This notice grants no additional license rights and does not claim that the hash verification establishes account entitlement, model availability, or billing behavior.

中文：显式构建 Antigravity ACP 镜像时，会从 Google 官方 `dl.google.com` 下载 1.4.5 版独立 ACP server，并在写入镜像前核对对应架构的 SHA-256。该专有可执行文件不会提交到本仓库。生成的镜像包含第三方可执行文件，使用和分发须遵守 Google 适用条款。本说明不授予额外许可，也不表示哈希校验能够证明账号权限、模型可用性或计费行为。

Google registry 为 Linux 指定可执行文件 `./agy_acp_server.par` 与固定参数 `--uid=`；该参数由 Navigator 提供，Compose 不接受调用方传入命令或 shell 字符串。两个 Linux archive 还包含同目录可执行 sidecar `localharness_external`；镜像将两个文件都以 0555 权限安装在 `/usr/local/bin`，以保留相对路径查找。该 sidecar 未压缩大小为 x86_64 133,926,136 字节、ARM64 127,512,832 字节。
