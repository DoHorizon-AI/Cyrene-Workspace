#!/usr/bin/env bash
# 打包 Cyrene 为可安装的 Ubuntu 24.04 .deb
# 用法: ./build-deb.sh --version 0.1.0-rc.1 --output ./dist/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION="0.1.0-rc.1"
OUTPUT_DIR="${WORKSPACE_ROOT}/dist"
ARCH="amd64"
SERVICE_WHEELHOUSE="${CYRENE_SERVICE_WHEELHOUSE:-${SCRIPT_DIR}/service-wheelhouse}"

print_help() {
    cat <<EOF
Usage: $0 [OPTIONS]

Build a Debian/Ubuntu .deb package for Cyrene.

Options:
  --version <version>   Package version (default: 0.1.0-rc.1)
  --output <dir>        Output directory for .deb package (default: ./dist/)
  --arch <arch>         Architecture (amd64 only for this release)
  --service-wheelhouse <dir>
                        Offline five-service wheelhouse (default: packaging/service-wheelhouse)
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
        --service-wheelhouse)
            SERVICE_WHEELHOUSE="$2"
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

if [[ "${ARCH}" != "amd64" ]]; then
    echo "ERROR: release-lock.json supports Ubuntu 24.04 x86_64 (amd64) only; refusing --arch ${ARCH}." >&2
    exit 2
fi

if ! command -v dpkg >/dev/null 2>&1; then
    echo "ERROR: dpkg is required to verify the native Debian build architecture." >&2
    exit 2
fi
HOST_ARCH="$(dpkg --print-architecture)"
if [[ "${HOST_ARCH}" != "amd64" ]]; then
    echo "ERROR: this RC supports native amd64 builds only; build host reports ${HOST_ARCH}." >&2
    exit 2
fi

if [[ ! -d "${SERVICE_WHEELHOUSE}" ]]; then
    echo "ERROR: service wheelhouse is missing: ${SERVICE_WHEELHOUSE}" >&2
    echo "Build a pinned offline wheelhouse first; see ${SCRIPT_DIR}/service-bundle.md." >&2
    echo "The Debian package will not contain non-runnable placeholder services." >&2
    exit 2
fi

mkdir -p "${OUTPUT_DIR}"
STAGE_DIR="$(mktemp -d -t cyrene-deb-XXXXXX)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

echo "==> Staging Cyrene .deb package (version: ${VERSION}, arch: ${ARCH})..."

# Directory structure
mkdir -p "${STAGE_DIR}/DEBIAN"
mkdir -p "${STAGE_DIR}/usr/bin"
mkdir -p "${STAGE_DIR}/usr/libexec"
mkdir -p "${STAGE_DIR}/usr/lib/cyrene"
mkdir -p "${STAGE_DIR}/etc/cyrene"
mkdir -p "${STAGE_DIR}/lib/systemd/system"
mkdir -p "${STAGE_DIR}/var/lib/cyrene"
mkdir -p "${STAGE_DIR}/usr/share/cyrene/service-artifacts"
mkdir -p "${STAGE_DIR}/usr/share/cyrene"
mkdir -p "${STAGE_DIR}/usr/share/polkit-1/actions"

mkdir -p "${STAGE_DIR}/usr/lib/cyrene/scripts"

# 1. CLI lives beside the other scripts under /usr/lib/cyrene, and /usr/bin/cyrene
#    is a symlink. The CLI derives its workspace root from its own location, so
#    installing the real file elsewhere made every data path resolve to the wrong
#    directory.
cp "${WORKSPACE_ROOT}/cyrene" "${STAGE_DIR}/usr/lib/cyrene/scripts/cyrene"
chmod 755 "${STAGE_DIR}/usr/lib/cyrene/scripts/cyrene"
ln -s /usr/lib/cyrene/scripts/cyrene "${STAGE_DIR}/usr/bin/cyrene"

# Polkit executes a root-owned fixed wrapper. The JSON request can select only
# an updater operation; it cannot change the executable or its arguments.
cp "${SCRIPT_DIR}/cyrene-component-update-helper" \
    "${STAGE_DIR}/usr/libexec/cyrene-component-update-helper"
chmod 755 "${STAGE_DIR}/usr/libexec/cyrene-component-update-helper"
cp "${SCRIPT_DIR}/org.cyrene.component-update.policy" \
    "${STAGE_DIR}/usr/share/polkit-1/actions/org.cyrene.component-update.policy"
chmod 644 "${STAGE_DIR}/usr/share/polkit-1/actions/org.cyrene.component-update.policy"

# 2. /usr/lib/cyrene/ -> Python runtime (managed by uv) & helper scripts
cp -r "${WORKSPACE_ROOT}/scripts/." "${STAGE_DIR}/usr/lib/cyrene/scripts/"
cp "${SCRIPT_DIR}/service_bundle.py" "${STAGE_DIR}/usr/lib/cyrene/scripts/service_bundle.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/service_bundle.py"
cp "${SCRIPT_DIR}/component_updates.py" "${STAGE_DIR}/usr/lib/cyrene/scripts/component_updates.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/component_updates.py"
cp "${SCRIPT_DIR}/catalog_metadata.py" "${STAGE_DIR}/usr/lib/cyrene/scripts/catalog_metadata.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/catalog_metadata.py"
cp "${SCRIPT_DIR}/component-catalog-bootstrap-v1.json" "${STAGE_DIR}/usr/share/cyrene/component-catalog-v1.json"
chmod 644 "${STAGE_DIR}/usr/share/cyrene/component-catalog-v1.json"
mkdir -p "${STAGE_DIR}/usr/share/cyrene/catalog-schemas"
cp "${WORKSPACE_ROOT}/governance/component-catalog-v1.schema.json" \
    "${STAGE_DIR}/usr/share/cyrene/catalog-schemas/component-catalog-v1.schema.json"
cp "${WORKSPACE_ROOT}/governance/component-release-manifest-v1.schema.json" \
    "${STAGE_DIR}/usr/share/cyrene/catalog-schemas/component-release-manifest-v1.schema.json"
cp "${WORKSPACE_ROOT}/governance/component-release-manifest-v2.schema.json" \
    "${STAGE_DIR}/usr/share/cyrene/catalog-schemas/component-release-manifest-v2.schema.json"
chmod 644 "${STAGE_DIR}/usr/share/cyrene/catalog-schemas/"*.schema.json
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

# Every managed process ships as an immutable release bundle. The helper checks
# the accepted source SHA from release-lock.json, the complete hash-pinned
# offline wheel set, and the bundle file manifest before the .deb is assembled.
python3 "${SCRIPT_DIR}/service_bundle.py" build \
    --wheelhouse "${SERVICE_WHEELHOUSE}" \
    --release-lock "${WORKSPACE_ROOT}/release-lock.json" \
    --output "${STAGE_DIR}/usr/share/cyrene/service-artifacts" \
    --arch "${ARCH}"

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
EnvironmentFile=/etc/cyrene/runtime-activity-sources.env
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_ID=cyrene-navigator
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE=%d/activity-token
Environment=CYRENE_RUNTIME_MAINTENANCE_SOCKET=/run/cyrene/runtime-maintenance.sock
LoadCredential=activity-token:/etc/cyrene/runtime-activity-source-tokens/cyrene-navigator.token
PrivateMounts=yes
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service
ExecStart=/usr/bin/cyrene service-run navigator
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
EnvironmentFile=/etc/cyrene/runtime-activity-sources.env
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_ID=cyrene-yield
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE=%d/activity-token
Environment=CYRENE_RUNTIME_MAINTENANCE_SOCKET=/run/cyrene/runtime-maintenance.sock
LoadCredential=activity-token:/etc/cyrene/runtime-activity-source-tokens/cyrene-yield.token
PrivateMounts=yes
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service
ExecStart=/usr/bin/cyrene service-run yield
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
EnvironmentFile=/etc/cyrene/runtime-activity-sources.env
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_ID=cyrene-reactor
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE=%d/activity-token
Environment=CYRENE_RUNTIME_MAINTENANCE_SOCKET=/run/cyrene/runtime-maintenance.sock
LoadCredential=activity-token:/etc/cyrene/runtime-activity-source-tokens/cyrene-reactor.token
PrivateMounts=yes
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service
ExecStart=/usr/bin/cyrene service-run reactor
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
EnvironmentFile=/etc/cyrene/runtime-activity-sources.env
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_ID=cyrene-exchange
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE=%d/activity-token
Environment=CYRENE_RUNTIME_MAINTENANCE_SOCKET=/run/cyrene/runtime-maintenance.sock
LoadCredential=activity-token:/etc/cyrene/runtime-activity-source-tokens/cyrene-exchange.token
PrivateMounts=yes
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service
ExecStart=/usr/bin/cyrene service-run exchange
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
EnvironmentFile=/etc/cyrene/runtime-activity-sources.env
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_ID=cyrene-catalyst
Environment=CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE=%d/activity-token
Environment=CYRENE_RUNTIME_MAINTENANCE_SOCKET=/run/cyrene/runtime-maintenance.sock
LoadCredential=activity-token:/etc/cyrene/runtime-activity-source-tokens/cyrene-catalyst.token
PrivateMounts=yes
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service
ExecStart=/usr/bin/cyrene service-run catalyst
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
Depends: python3 (>= 3.12), python3 (<< 3.13), python3-jsonschema (>= 4.0), systemd, policykit-1, acl
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

# Product contract data has separate read domains: Authority can read source
# metadata and plans, while the BFF can read only immutable activated versions.
for bundle_group in cyrene-authority cyrene-product-bundle-reader; do
    if ! getent group "$bundle_group" >/dev/null 2>&1; then
        groupadd --system "$bundle_group"
    fi
done
ensure_bundle_directory() {
    path="$1"
    owner_group="$2"
    mode="$3"
    if [ -L "$path" ]; then
        echo "ERROR: $path is a symlink; refusing to alter Product bundle trust data." >&2
        exit 1
    elif [ ! -e "$path" ]; then
        install -d -o root -g "$owner_group" -m "$mode" "$path"
    fi
    expected="0:$(getent group "$owner_group" | cut -d: -f3):$mode"
    actual="$(stat -c '%u:%g:%a' -- "$path")"
    if [ ! -d "$path" ] || [ "$actual" != "$expected" ]; then
        echo "ERROR: $path must be $expected; found $actual. Refusing to repair trusted state in place." >&2
        exit 1
    fi
}
bundle_root=/var/lib/cyrene-product-bundles
if [ -L "$bundle_root" ]; then
    echo "ERROR: $bundle_root is a symlink; refusing to alter Product bundle trust data." >&2
    exit 1
elif [ ! -e "$bundle_root" ]; then
    install -d -o root -g root -m 0700 "$bundle_root"
fi
if [ ! -d "$bundle_root" ] || [ "$(stat -c '%u:%g' -- "$bundle_root")" != "0:0" ]; then
    echo "ERROR: $bundle_root must be a root-owned directory." >&2
    exit 1
fi
AUTHORITY_BUNDLE_GID="$(getent group cyrene-authority | cut -d: -f3)"
BUNDLE_READER_GID="$(getent group cyrene-product-bundle-reader | cut -d: -f3)"
setfacl -m "u::rwx,g::---,g:${AUTHORITY_BUNDLE_GID}:--x,g:${BUNDLE_READER_GID}:--x,m::--x,o::---" "$bundle_root"
if [ "$(stat -c '%u:%g:%a' -- "$bundle_root")" != "0:0:710" ] \
    || ! getfacl -cpn -- "$bundle_root" | grep -Fxq "group:${AUTHORITY_BUNDLE_GID}:--x" \
    || ! getfacl -cpn -- "$bundle_root" | grep -Fxq "group:${BUNDLE_READER_GID}:--x"; then
    echo "ERROR: $bundle_root traversal ACL does not match the two trusted service groups." >&2
    exit 1
fi
ensure_bundle_directory /var/lib/cyrene-product-bundles/archives cyrene-authority 750
ensure_bundle_directory /var/lib/cyrene-product-bundles/metadata cyrene-authority 750
ensure_bundle_directory /var/lib/cyrene-product-bundles/versions cyrene-product-bundle-reader 750

# 设置数据目录与配置目录权限
if [ -L /var/lib/cyrene ]; then
    echo "ERROR: /var/lib/cyrene is a symlink; refusing to change runtime state through it." >&2
    exit 1
fi
mkdir -p /var/lib/cyrene /var/log/cyrene /etc/cyrene
chown cyrene:cyrene /var/lib/cyrene /var/log/cyrene
find /var/lib/cyrene -mindepth 1 -maxdepth 1 ! -name runtime \
    -exec chown -hR cyrene:cyrene -- {} +
chmod 750 /var/lib/cyrene /var/log/cyrene

# The Platform broker owns the durable runtime catalog and maintenance journal.
# Require its authority group and binary before Product units are admitted.
if ! getent group cyrene-runtime-maintenance >/dev/null 2>&1; then
    echo "ERROR: cyrene-runtime-maintenance authority group is missing; install the verified Platform runtime-maintenance component first." >&2
    exit 1
fi
if [ ! -x /usr/bin/cyrene-runtime-maintenance ]; then
    echo "ERROR: /usr/bin/cyrene-runtime-maintenance is missing; install the verified Platform runtime-maintenance component first." >&2
    exit 1
fi
if ! /usr/bin/cyrene component-install-missing-units; then
    echo "ERROR: could not install or validate catalog-pinned Platform systemd units." >&2
    exit 1
fi
if [ ! -f /lib/systemd/system/cyrene-runtime-maintenance.service ] && [ ! -f /usr/lib/systemd/system/cyrene-runtime-maintenance.service ] && [ ! -f /etc/systemd/system/cyrene-runtime-maintenance.service ]; then
    echo "ERROR: cyrene-runtime-maintenance.service is missing; install the verified Platform runtime-maintenance component first." >&2
    exit 1
fi

CYRENE_UID="$(id -u cyrene)"
CYRENE_GID="$(id -g cyrene)"
AUTHORITY_GID="$(getent group cyrene-runtime-maintenance | cut -d: -f3)"
AUTHORITY_IDENTITY_TMP="$(mktemp /etc/cyrene/.workspace-authority-identity.env.XXXXXX)"
printf 'CYRENE_AUTHORITY_BFF_PEER_UID=%s\n' "$CYRENE_UID" > "$AUTHORITY_IDENTITY_TMP"
chown root:root "$AUTHORITY_IDENTITY_TMP"
chmod 644 "$AUTHORITY_IDENTITY_TMP"
mv -f "$AUTHORITY_IDENTITY_TMP" /etc/cyrene/workspace-authority-identity.env
RUNTIME_STATE_DIR=/var/lib/cyrene/runtime
if [ -L "$RUNTIME_STATE_DIR" ]; then
    echo "ERROR: $RUNTIME_STATE_DIR is a symlink; refusing to follow it." >&2
    exit 1
elif [ -e "$RUNTIME_STATE_DIR" ]; then
    if [ ! -d "$RUNTIME_STATE_DIR" ]; then
        echo "ERROR: $RUNTIME_STATE_DIR exists but is not a directory." >&2
        exit 1
    fi
    RUNTIME_STATE_OWNER_GROUP_MODE="$(stat -c '%u:%g:%a' -- "$RUNTIME_STATE_DIR")"
    if [ "$RUNTIME_STATE_OWNER_GROUP_MODE" != "0:${AUTHORITY_GID}:2770" ]; then
        echo "ERROR: $RUNTIME_STATE_DIR must be root:cyrene-runtime-maintenance mode 2770; found ${RUNTIME_STATE_OWNER_GROUP_MODE}." >&2
        echo "       Refusing to repair existing runtime state in place. Stop Cyrene and repair the directory during offline maintenance." >&2
        exit 1
    fi
else
    install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"
fi
RUNTIME_STATE_OWNER_GROUP_MODE="$(stat -c '%u:%g:%a' -- "$RUNTIME_STATE_DIR")"
if [ -L "$RUNTIME_STATE_DIR" ] || [ "$RUNTIME_STATE_OWNER_GROUP_MODE" != "0:${AUTHORITY_GID}:2770" ]; then
    echo "ERROR: $RUNTIME_STATE_DIR is not a root-owned 2770 authority directory." >&2
    exit 1
fi
ACTIVITY_INIT_JSON="$(/usr/bin/cyrene-runtime-maintenance init-catalog \
    --catalog "$RUNTIME_STATE_DIR/activity-sources.json" \
    --token-dir /etc/cyrene/runtime-activity-source-tokens \
    --catalog-gid "${AUTHORITY_GID}" \
    --source "cyrene-navigator=${CYRENE_UID}:${CYRENE_GID}" \
    --source "cyrene-yield=${CYRENE_UID}:${CYRENE_GID}" \
    --source "cyrene-reactor=${CYRENE_UID}:${CYRENE_GID}" \
    --source "cyrene-exchange=${CYRENE_UID}:${CYRENE_GID}" \
    --source "cyrene-catalyst=${CYRENE_UID}:${CYRENE_GID}")"
ACTIVITY_GENERATION="$(printf '%s' "$ACTIVITY_INIT_JSON" | /usr/bin/python3.12 -c 'import json,sys; value=json.load(sys.stdin); generation=value.get("generation"); print(generation) if isinstance(generation,int) and not isinstance(generation,bool) and generation > 0 else sys.exit("invalid activity catalog generation")')"
case "$ACTIVITY_GENERATION" in
    ''|*[!0-9]*)
        echo "ERROR: runtime-maintenance init-catalog returned an invalid generation." >&2
        exit 1
        ;;
esac
ACTIVITY_ENV_TMP="$(mktemp /etc/cyrene/.runtime-activity-sources.env.XXXXXX)"
printf 'CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=%s\n' "$ACTIVITY_GENERATION" > "$ACTIVITY_ENV_TMP"
chown root:root "$ACTIVITY_ENV_TMP"
chmod 644 "$ACTIVITY_ENV_TMP"
mv -f "$ACTIVITY_ENV_TMP" /etc/cyrene/runtime-activity-sources.env

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

# Validate and stage all five release bundles. Existing active symlinks are
# retained on upgrade; the CLI activates only missing releases on first install.
/usr/bin/cyrene service-bootstrap --activate-missing

# 仅首次安装（而非升级）时生成 cyrene 用户拥有的配对凭据。
if [ "$1" = "configure" ] && [ -z "${2:-}" ]; then
    runuser -u cyrene -- env CYRENE_DEV_HOME=/var/lib/cyrene CYRENE_DATA_DIR=/var/lib/cyrene /usr/bin/cyrene init
fi

# Reload unit definitions on install and upgrade. Preserve the administrator's
# enabled/running state on upgrades; a first install enables and starts units.
SYSTEMD_RUNNING=0
if [ -d /run/systemd/system ]; then
    SYSTEMD_RUNNING=1
    if ! systemctl daemon-reload; then
        echo "ERROR: systemd could not reload the installed Cyrene units." >&2
        exit 1
    fi
    if [ "$1" = "configure" ] && [ -z "${2:-}" ]; then
        if ! systemctl enable cyrene-runtime-maintenance.service; then
            echo "ERROR: systemd could not enable cyrene-runtime-maintenance.service." >&2
            exit 1
        fi
    fi
    if ! systemctl start cyrene-runtime-maintenance.service; then
        echo "ERROR: runtime maintenance broker failed to start; Product services will not be started." >&2
        systemctl status --no-pager --full cyrene-runtime-maintenance.service >&2 || true
        journalctl --no-pager -n 40 -u cyrene-runtime-maintenance.service >&2 || true
        exit 1
    fi
    if ! systemctl is-active --quiet cyrene-runtime-maintenance.service; then
        echo "ERROR: runtime maintenance broker is not active; Product services will not be started." >&2
        exit 1
    fi
    if [ "$1" = "configure" ] && [ -z "${2:-}" ]; then
        for unit in cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst; do
            if ! systemctl enable "$unit"; then
                echo "ERROR: systemd could not enable ${unit}.service." >&2
                exit 1
            fi
        done
        for unit in cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst; do
            if ! systemctl start "$unit"; then
                echo "ERROR: initial start failed for ${unit}.service; package configuration is incomplete." >&2
                systemctl status --no-pager --full "$unit" >&2 || true
                journalctl --no-pager -n 40 -u "$unit" >&2 || true
                exit 1
            fi
        done
        for unit in cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst; do
            if ! systemctl is-active --quiet "$unit"; then
                echo "ERROR: ${unit}.service exited during initial startup; package configuration is incomplete." >&2
                systemctl status --no-pager --full "$unit" >&2 || true
                journalctl --no-pager -n 40 -u "$unit" >&2 || true
                exit 1
            fi
        done
    fi
else
    if [ "$1" = "configure" ] && [ -z "${2:-}" ]; then
        if ! systemctl --root=/ enable cyrene-runtime-maintenance.service; then
            echo "ERROR: systemd could not enable cyrene-runtime-maintenance.service for the next boot." >&2
            exit 1
        fi
        for unit in cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst; do
            if ! systemctl --root=/ enable "${unit}.service"; then
                echo "ERROR: systemd could not enable ${unit}.service for the next boot." >&2
                exit 1
            fi
        done
    fi
    echo "WARNING: systemd is not running; Cyrene units were installed but not started." >&2
fi

if [ "$SYSTEMD_RUNNING" -eq 1 ]; then
    echo "Cyrene installed successfully."
else
    echo "Cyrene package files are installed; systemd is not running, so services were not started."
fi
echo "Run 'cyrene init' or 'cyrene doctor' to get started."
exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/postinst"

# Stop and disable services only when the package is being removed. During
# upgrades dpkg passes "upgrade", so currently running services stay untouched.
cat <<'EOF' > "${STAGE_DIR}/DEBIAN/prerm"
#!/bin/sh
set -e

if [ "${1:-}" = "remove" ]; then
    if [ -d /run/systemd/system ]; then
        for unit in cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst; do
            if ! systemctl stop "${unit}.service"; then
                echo "ERROR: could not stop ${unit}.service before removing Cyrene." >&2
                exit 1
            fi
            if ! systemctl disable "${unit}.service"; then
                echo "ERROR: could not disable ${unit}.service before removing Cyrene." >&2
                exit 1
            fi
        done
    else
        echo "WARNING: systemd is not running; Cyrene services could not be stopped." >&2
        for unit in cyrene-navigator cyrene-yield cyrene-reactor cyrene-exchange cyrene-catalyst; do
            if ! systemctl --root=/ disable "${unit}.service"; then
                echo "ERROR: systemd could not disable ${unit}.service from the offline root." >&2
                exit 1
            fi
        done
    fi
fi

exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/prerm"

# Remove stale unit definitions after dpkg removes the unit files. Keep the
# service release history and /var/lib/cyrene data for a possible reinstall.
cat <<'EOF' > "${STAGE_DIR}/DEBIAN/postrm"
#!/bin/sh
set -e

case "${1:-}" in
    remove|purge)
        if [ -d /run/systemd/system ]; then
            if ! systemctl daemon-reload; then
                echo "ERROR: systemd could not reload after removing Cyrene units." >&2
                exit 1
            fi
        fi
        ;;
esac

exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/postrm"

# 7. Build .deb package
OUTPUT_PACKAGE="${OUTPUT_DIR}/cyrene_${VERSION}_${ARCH}.deb"
echo "==> Building package with dpkg-deb: ${OUTPUT_PACKAGE}..."
dpkg-deb --build --root-owner-group "${STAGE_DIR}" "${OUTPUT_PACKAGE}"

echo "==> Package built successfully:"
ls -lh "${OUTPUT_PACKAGE}"
dpkg-deb -I "${OUTPUT_PACKAGE}"
