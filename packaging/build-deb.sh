#!/usr/bin/env bash
# 打包 Cyrene 为可安装的 Ubuntu 24.04 .deb
# 用法: ./build-deb.sh --version 0.1.0-rc.1 --output ./dist/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION="0.1.0-rc.1"
OUTPUT_DIR="${WORKSPACE_ROOT}/dist"
ARCH="amd64"

print_help() {
    cat <<EOF
Usage: $0 [OPTIONS]

Build a Debian/Ubuntu .deb package for Cyrene.

Options:
  --version <version>   Package version (default: 0.1.0-rc.1)
  --output <dir>        Output directory for .deb package (default: ./dist/)
  --arch <arch>         Architecture (default: amd64)
  -h, --help            Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --arch)
            ARCH="$2"
            shift 2
            ;;
        -h|--help)
            print_help
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            print_help
            exit 1
            ;;
    esac
done

mkdir -p "${OUTPUT_DIR}"
STAGE_DIR="$(mktemp -d -t cyrene-deb-XXXXXX)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

echo "==> Staging Cyrene .deb package (version: ${VERSION}, arch: ${ARCH})..."

# Directory structure
mkdir -p "${STAGE_DIR}/DEBIAN"
mkdir -p "${STAGE_DIR}/usr/bin"
mkdir -p "${STAGE_DIR}/usr/lib/cyrene"
mkdir -p "${STAGE_DIR}/etc/cyrene"
mkdir -p "${STAGE_DIR}/lib/systemd/system"
mkdir -p "${STAGE_DIR}/var/lib/cyrene"

mkdir -p "${STAGE_DIR}/usr/lib/cyrene/scripts"

# 1. CLI lives beside the other scripts under /usr/lib/cyrene, and /usr/bin/cyrene
#    is a symlink. The CLI derives its workspace root from its own location, so
#    installing the real file elsewhere made every data path resolve to the wrong
#    directory.
cp "${WORKSPACE_ROOT}/cyrene" "${STAGE_DIR}/usr/lib/cyrene/scripts/cyrene"
chmod 755 "${STAGE_DIR}/usr/lib/cyrene/scripts/cyrene"
ln -s /usr/lib/cyrene/scripts/cyrene "${STAGE_DIR}/usr/bin/cyrene"

# 2. /usr/lib/cyrene/ -> Python runtime (managed by uv) & helper scripts
cp -r "${WORKSPACE_ROOT}/scripts/." "${STAGE_DIR}/usr/lib/cyrene/scripts/"
if [[ -f "${WORKSPACE_ROOT}/pyproject.toml" ]]; then
    cp "${WORKSPACE_ROOT}/pyproject.toml" "${STAGE_DIR}/usr/lib/cyrene/"
fi
if [[ -f "${WORKSPACE_ROOT}/uv.lock" ]]; then
    cp "${WORKSPACE_ROOT}/uv.lock" "${STAGE_DIR}/usr/lib/cyrene/"
fi
# Ships the pinned engine versions and repository revisions so `cyrene doctor`
# and the installation flows read pins from the package instead of inventing them.
if [[ -f "${WORKSPACE_ROOT}/release-lock.json" ]]; then
    cp "${WORKSPACE_ROOT}/release-lock.json" "${STAGE_DIR}/usr/lib/cyrene/"
fi
# Runtime bootstrap reads those pins and installs the engines into a venv.
cp "${SCRIPT_DIR}/bootstrap.sh" "${STAGE_DIR}/usr/lib/cyrene/bootstrap.sh"
chmod 755 "${STAGE_DIR}/usr/lib/cyrene/bootstrap.sh"
# The Web Host launcher: `cyrene up` starts it to serve the console API.
for candidate in \
    "${WORKSPACE_ROOT}/../Cyrene-Services/Cyrene-Navigator" \
    "${WORKSPACE_ROOT}/../Cyrene-Navigator"; do
    [[ -f "${candidate}/scripts/serve-web.py" ]] || continue
    cp "${candidate}/scripts/serve-web.py" "${STAGE_DIR}/usr/lib/cyrene/scripts/serve-web.py"
    chmod 755 "${STAGE_DIR}/usr/lib/cyrene/scripts/serve-web.py"
    break
done

# 3. /etc/cyrene/ -> Default configuration template
cat <<'EOF' > "${STAGE_DIR}/etc/cyrene/cyrene.env"
# Cyrene Default System Configuration
CYRENE_DOMAIN=localhost
CYRENE_DEV_HOME=/var/lib/cyrene
CYRENE_DATA_DIR=/var/lib/cyrene
CYRENE_LOGS_DIR=/var/log/cyrene

# Product Port Assignments
CYRENE_PORT_NAVIGATOR=7860
CYRENE_PORT_YIELD=8001
CYRENE_PORT_REACTOR=8002
CYRENE_PORT_EXCHANGE=8003
CYRENE_PORT_CATALYST=8004
EOF

if [[ -f "${SCRIPT_DIR}/Caddyfile.template" ]]; then
    cp "${SCRIPT_DIR}/Caddyfile.template" "${STAGE_DIR}/etc/cyrene/Caddyfile.template"
fi

# 4. Systemd service units
# cyrene-navigator.service
cat <<'EOF' > "${STAGE_DIR}/lib/systemd/system/cyrene-navigator.service"
[Unit]
Description=Cyrene Navigator Web Host
After=network.target

[Service]
Type=simple
User=cyrene
Group=cyrene
WorkingDirectory=/var/lib/cyrene
EnvironmentFile=-/etc/cyrene/cyrene.env
ExecStart=/usr/bin/cyrene up
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

# cyrene-yield.service
cat <<'EOF' > "${STAGE_DIR}/lib/systemd/system/cyrene-yield.service"
[Unit]
Description=Cyrene Yield Training Engine Service
After=network.target

[Service]
Type=simple
User=cyrene
Group=cyrene
WorkingDirectory=/var/lib/cyrene
EnvironmentFile=-/etc/cyrene/cyrene.env
ExecStart=/usr/bin/python3 -m cy_exec.training.product_cli --port 8001
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# cyrene-reactor.service
cat <<'EOF' > "${STAGE_DIR}/lib/systemd/system/cyrene-reactor.service"
[Unit]
Description=Cyrene Reactor Serving Engine Service
After=network.target

[Service]
Type=simple
User=cyrene
Group=cyrene
WorkingDirectory=/var/lib/cyrene
EnvironmentFile=-/etc/cyrene/cyrene.env
ExecStart=/usr/bin/python3 -m cyrene_reactor_product.cli control --port 8002
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# cyrene-exchange.service
cat <<'EOF' > "${STAGE_DIR}/lib/systemd/system/cyrene-exchange.service"
[Unit]
Description=Cyrene Exchange Routing & Gateway Service
After=network.target

[Service]
Type=simple
User=cyrene
Group=cyrene
WorkingDirectory=/var/lib/cyrene
EnvironmentFile=-/etc/cyrene/cyrene.env
ExecStart=/usr/bin/python3 -m cyrene_exchange_product.cli serve --port 8003
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# cyrene-catalyst.service
cat <<'EOF' > "${STAGE_DIR}/lib/systemd/system/cyrene-catalyst.service"
[Unit]
Description=Cyrene Catalyst Dataset Preparation Service
After=network.target

[Service]
Type=simple
User=cyrene
Group=cyrene
WorkingDirectory=/var/lib/cyrene
EnvironmentFile=-/etc/cyrene/cyrene.env
ExecStart=/usr/bin/python3 -m cyrene_catalyst --port 8004
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${STAGE_DIR}/lib/systemd/system/"*.service

# 5. DEBIAN/control
cat <<EOF > "${STAGE_DIR}/DEBIAN/control"
Package: cyrene
Version: ${VERSION}
Section: devel
Priority: optional
Architecture: ${ARCH}
Maintainer: Cyrene Team <team@cyrene.dev>
Description: Cyrene Unified Local LLM Stack
 Cyrene provides a complete local LLM development and inference platform,
 including model importation (Reactor), training drafts (Yield), dataset preparation
 (Catalyst), unified API gateway (Exchange), and same-origin console (Navigator).
EOF

# 6. DEBIAN/postinst
cat <<'EOF' > "${STAGE_DIR}/DEBIAN/postinst"
#!/bin/sh
set -e

# 创建 cyrene 系统用户
if ! id -u cyrene >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --shell /bin/false cyrene
fi

# 设置数据目录与配置目录权限
mkdir -p /var/lib/cyrene /var/log/cyrene /etc/cyrene
chown -R cyrene:cyrene /var/lib/cyrene /var/log/cyrene
chmod 750 /var/lib/cyrene /var/log/cyrene

# 安装 release-lock.json 锁定的 training/serving 引擎到隔离虚拟环境。
# 这一步需要网络，失败不应让 dpkg 安装失败——允许用户之后手动重跑。
if [ -x /usr/lib/cyrene/bootstrap.sh ]; then
    if /usr/lib/cyrene/bootstrap.sh; then
        echo "Cyrene runtime engines installed."
    else
        echo "WARNING: automatic engine installation failed." >&2
        echo "         Re-run '/usr/lib/cyrene/bootstrap.sh' once network access is available." >&2
    fi
else
    echo "WARNING: /usr/lib/cyrene/bootstrap.sh missing; engines were not installed." >&2
fi

# 重新加载 systemd units
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
    # 只有 systemd 实际可用时才启用并启动服务。
    systemctl enable cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst 2>/dev/null || true
    systemctl restart cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst 2>/dev/null || true
fi

# 仅首次安装（而非升级）时生成配对凭据
if [ "$1" = "configure" ] && [ -z "$2" ]; then
    /usr/bin/cyrene init || true
fi

echo "Cyrene installed successfully."
echo "Run 'cyrene init' or 'cyrene doctor' to get started."
exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/postinst"

# 7. Build .deb package
OUTPUT_PACKAGE="${OUTPUT_DIR}/cyrene_${VERSION}_${ARCH}.deb"
echo "==> Building package with dpkg-deb: ${OUTPUT_PACKAGE}..."
dpkg-deb --build --root-owner-group "${STAGE_DIR}" "${OUTPUT_PACKAGE}"

echo "==> Package built successfully:"
ls -lh "${OUTPUT_PACKAGE}"
dpkg-deb -I "${OUTPUT_PACKAGE}"
