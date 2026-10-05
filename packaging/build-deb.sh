#!/usr/bin/env bash
# Assemble Cyrene's native Ubuntu package from a locked private Python runtime
# and either attested Product releases or an explicitly non-release source build.

set -euo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PACKAGE_VERSION="0.1.0-rc.1"
OUTPUT_DIR="${WORKSPACE_ROOT}/dist"
ARCH="amd64"
TARGET_PROFILE=""
SERVICE_WHEELHOUSE="${CYRENE_SERVICE_WHEELHOUSE:-${SCRIPT_DIR}/service-wheelhouse}"
VERIFIED_SERVICE_ARTIFACTS=""
PYTHON_RUNTIME_ARCHIVE=""
UV_EXECUTABLE=""
DEVELOPMENT_SOURCE_BUILD=0

print_help() {
    cat <<EOF
Usage: $0 --target-profile <targetId> --python-runtime-archive <archive> [OPTIONS]

Assemble an Ubuntu .deb package for Cyrene.

Options:
  --version <version>   Package version (default: 0.1.0-rc.1)
  --output <dir>        Output directory for .deb package (default: ./dist/)
  --arch <arch>         Architecture (amd64 only for this release)
  --target-profile <id> Exact Linux Ubuntu Python profile from release-lock.json
  --python-runtime-archive <file>
                        Official pinned CPython 3.12.14 PBS archive; SHA-256 is checked
  --uv-executable <file>
                        Optional verified uv 0.12.21 build executable; it is also staged at its locked runtime path
  --verified-service-artifacts <dir>
                        Index plus original Product tar/manifest/attestation release assets
  --development-source-build
                        Build service bundles from a wheelhouse for local development only
  --service-wheelhouse <dir>
                        Offline five-service wheelhouse (development mode only)
  -h, --help            Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            PACKAGE_VERSION="$2"
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
        --target-profile)
            TARGET_PROFILE="$2"
            shift 2
            ;;
        --python-runtime-archive)
            PYTHON_RUNTIME_ARCHIVE="$2"
            shift 2
            ;;
        --uv-executable)
            UV_EXECUTABLE="$2"
            shift 2
            ;;
        --verified-service-artifacts)
            VERIFIED_SERVICE_ARTIFACTS="$2"
            shift 2
            ;;
        --development-source-build)
            DEVELOPMENT_SOURCE_BUILD=1
            shift
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
    echo "ERROR: release-lock.json supports Ubuntu 22.04/24.04 x86_64 (amd64) only; refusing --arch ${ARCH}." >&2
    exit 2
fi
if [[ ! "${PACKAGE_VERSION}" =~ ^[0-9A-Za-z][0-9A-Za-z.+:~_-]{0,127}$ ]]; then
    echo "ERROR: --version must be a single safe Debian version token." >&2
    exit 2
fi

if [[ "${TARGET_PROFILE}" != "linux-ubuntu-22.04-x86_64-python-3.12" \
    && "${TARGET_PROFILE}" != "linux-ubuntu-24.04-x86_64-python-3.12" ]]; then
    echo "ERROR: --target-profile must be an exact supported Ubuntu Python 3.12 profile." >&2
    exit 2
fi

if [[ -n "${VERIFIED_SERVICE_ARTIFACTS}" && "${DEVELOPMENT_SOURCE_BUILD}" -eq 1 ]]; then
    echo "ERROR: --verified-service-artifacts cannot be combined with --development-source-build." >&2
    exit 2
fi
if [[ -n "${VERIFIED_SERVICE_ARTIFACTS}" && -n "${CYRENE_SERVICE_WHEELHOUSE:-}" ]]; then
    echo "ERROR: CYRENE_SERVICE_WHEELHOUSE is not accepted in verified published-artifact mode." >&2
    exit 2
fi
if [[ -n "${VERIFIED_SERVICE_ARTIFACTS}" ]]; then
    if [[ -L "${VERIFIED_SERVICE_ARTIFACTS}" || ! -d "${VERIFIED_SERVICE_ARTIFACTS}" \
        || -L "${VERIFIED_SERVICE_ARTIFACTS}/index.json" || ! -f "${VERIFIED_SERVICE_ARTIFACTS}/index.json" ]]; then
        echo "ERROR: --verified-service-artifacts must be a directory with a regular index.json." >&2
        exit 2
    fi
fi
if [[ -z "${VERIFIED_SERVICE_ARTIFACTS}" && "${DEVELOPMENT_SOURCE_BUILD}" -ne 1 ]]; then
    echo "ERROR: choose --verified-service-artifacts for release assembly or explicitly opt into --development-source-build." >&2
    exit 2
fi
if [[ -z "${PYTHON_RUNTIME_ARCHIVE}" ]]; then
    echo "ERROR: --python-runtime-archive is required; provide the official archive verified by python_runtime.py." >&2
    exit 2
fi
if [[ ! -f "${PYTHON_RUNTIME_ARCHIVE}" || -L "${PYTHON_RUNTIME_ARCHIVE}" ]]; then
    echo "ERROR: Python runtime archive is missing or unsafe: ${PYTHON_RUNTIME_ARCHIVE}" >&2
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
case "${TARGET_PROFILE}" in
    linux-ubuntu-22.04-x86_64-python-3.12)
        EXPECTED_UBUNTU_VERSION="22.04"
        MINIMUM_GLIBC="2.35"
        ;;
    linux-ubuntu-24.04-x86_64-python-3.12)
        EXPECTED_UBUNTU_VERSION="24.04"
        MINIMUM_GLIBC="2.39"
        ;;
esac
if [[ ! -r /etc/os-release ]]; then
    echo "ERROR: /etc/os-release is required to verify the exact native build profile." >&2
    exit 2
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "${EXPECTED_UBUNTU_VERSION}" ]]; then
    echo "ERROR: target profile ${TARGET_PROFILE} requires Ubuntu ${EXPECTED_UBUNTU_VERSION}; build host reports ${ID:-unknown} ${VERSION_ID:-unknown}." >&2
    exit 2
fi

if [[ "${DEVELOPMENT_SOURCE_BUILD}" -eq 1 && ! -d "${SERVICE_WHEELHOUSE}" ]]; then
    echo "ERROR: service wheelhouse is missing: ${SERVICE_WHEELHOUSE}" >&2
    echo "Build a pinned offline wheelhouse first; see ${SCRIPT_DIR}/service-bundle.md." >&2
    echo "The Debian package will not contain non-runnable placeholder services." >&2
    exit 2
fi

# Use the workflow-selected pinned helper interpreter instead of a host-specific path.
BUILD_PYTHON="$(command -v python3 2>/dev/null || true)"
if [[ -z "${BUILD_PYTHON}" || ! -x "${BUILD_PYTHON}" ]]; then
    echo "ERROR: Python 3.12.14 build helper is unavailable on PATH." >&2
    exit 2
fi
BUILD_PYTHON_VERSION="$("${BUILD_PYTHON}" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
if [[ "${BUILD_PYTHON_VERSION}" != "3.12.14" ]]; then
    echo "ERROR: build helpers require the selected Python 3.12.14, got ${BUILD_PYTHON_VERSION}." >&2
    exit 2
fi

mkdir -p "${OUTPUT_DIR}"
OUTPUT_PACKAGE="${OUTPUT_DIR}/cyrene_${PACKAGE_VERSION}_${ARCH}.deb"
if [[ -L "${OUTPUT_PACKAGE}" || -d "${OUTPUT_PACKAGE}" ]]; then
    echo "ERROR: package output must not be a symlink or directory: ${OUTPUT_PACKAGE}" >&2
    exit 2
fi
STAGE_DIR="$(mktemp -d -t cyrene-deb-XXXXXX)"
BUILD_WORK_DIR="$(mktemp -d -t cyrene-deb-work-XXXXXX)"
TEMP_OUTPUT_DIR="$(mktemp -d -p "${OUTPUT_DIR}" .cyrene-deb-build-XXXXXX)"
trap 'rm -rf "${STAGE_DIR}" "${BUILD_WORK_DIR}" "${TEMP_OUTPUT_DIR}"' EXIT

echo "==> Staging Cyrene .deb package (version: ${PACKAGE_VERSION}, arch: ${ARCH})..."

# Directory structure
mkdir -p "${STAGE_DIR}/DEBIAN"
mkdir -p "${STAGE_DIR}/usr/bin"
mkdir -p "${STAGE_DIR}/usr/libexec"
mkdir -p "${STAGE_DIR}/usr/lib/cyrene"
mkdir -p "${STAGE_DIR}/etc/cyrene"
mkdir -p "${STAGE_DIR}/lib/systemd/system"
mkdir -p "${STAGE_DIR}/usr/share/cyrene/service-artifacts"
mkdir -p "${STAGE_DIR}/usr/share/cyrene"
mkdir -p "${STAGE_DIR}/usr/lib/cyrene/packaging"
mkdir -p "${STAGE_DIR}/usr/share/polkit-1/actions"

mkdir -p "${STAGE_DIR}/usr/lib/cyrene/scripts"

# Stage the pinned private CPython and its offline runtime dependencies first.
# The build resolver and PBS archive are independently pinned by this lock.
PYTHON_RUNTIME_ARGS=(
    prepare
    --lock "${SCRIPT_DIR}/python-runtime.lock.json"
    --release-lock "${WORKSPACE_ROOT}/release-lock.json"
    --target-profile "${TARGET_PROFILE}"
    --stage-root "${STAGE_DIR}"
    --work-dir "${BUILD_WORK_DIR}/python-runtime"
    --archive "${PYTHON_RUNTIME_ARCHIVE}"
    --json
)
if [[ -n "${UV_EXECUTABLE}" ]]; then
    PYTHON_RUNTIME_ARGS+=(--uv-executable "${UV_EXECUTABLE}")
fi
"${BUILD_PYTHON}" "${SCRIPT_DIR}/python_runtime.py" "${PYTHON_RUNTIME_ARGS[@]}" \
    > "${BUILD_WORK_DIR}/python-runtime-receipt.json"
RUNTIME_PYTHON="${STAGE_DIR}/opt/cyrene/python/3.12.14/bin/python3.12"
[[ -x "${RUNTIME_PYTHON}" ]] || {
    echo "ERROR: locked private Python staging did not produce ${RUNTIME_PYTHON}." >&2
    exit 2
}

# Keep the Python entry point beneath the package install root; the public
# command wrapper and every systemd product unit name its interpreter explicitly.
cp "${WORKSPACE_ROOT}/cyrene" "${STAGE_DIR}/usr/lib/cyrene/scripts/cyrene.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/cyrene.py"
cat <<'EOF' > "${STAGE_DIR}/usr/bin/cyrene"
#!/bin/sh
set -eu
exec /opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py "$@"
EOF
chmod 755 "${STAGE_DIR}/usr/bin/cyrene"

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
cp "${SCRIPT_DIR}/native_package_runtime_bootstrap.py" "${STAGE_DIR}/usr/lib/cyrene/scripts/native_package_runtime_bootstrap.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/native_package_runtime_bootstrap.py"
cp "${SCRIPT_DIR}/catalog_metadata.py" "${STAGE_DIR}/usr/lib/cyrene/scripts/catalog_metadata.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/catalog_metadata.py"
cp "${SCRIPT_DIR}/native_component_bootstrap.py" \
    "${STAGE_DIR}/usr/lib/cyrene/scripts/native_component_bootstrap.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/native_component_bootstrap.py"
cp "${SCRIPT_DIR}/native_core_bootstrap.py" \
    "${STAGE_DIR}/usr/lib/cyrene/scripts/native_core_bootstrap.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/native_core_bootstrap.py"
cp "${SCRIPT_DIR}/native_runtime_schema_migration.py" \
    "${STAGE_DIR}/usr/lib/cyrene/scripts/native_runtime_schema_migration.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/native_runtime_schema_migration.py"
cp "${SCRIPT_DIR}/native_first_products.py" \
    "${STAGE_DIR}/usr/lib/cyrene/scripts/native_first_products.py"
chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/native_first_products.py"
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
cp "${SCRIPT_DIR}/python-runtime.lock.json" "${STAGE_DIR}/usr/lib/cyrene/packaging/"
cp "${SCRIPT_DIR}/python-runtime-requirements.lock" "${STAGE_DIR}/usr/lib/cyrene/packaging/"
cp "${SCRIPT_DIR}/bootstrap.sh" "${STAGE_DIR}/usr/lib/cyrene/bootstrap.sh"
chmod 755 "${STAGE_DIR}/usr/lib/cyrene/bootstrap.sh"

if [[ -n "${VERIFIED_SERVICE_ARTIFACTS}" ]]; then
    VERIFIED_STAGE="${BUILD_WORK_DIR}/verified-service-artifacts"
    "${BUILD_PYTHON}" "${SCRIPT_DIR}/verified_service_artifacts.py" \
        --input-root "${VERIFIED_SERVICE_ARTIFACTS}" \
        --target-profile "${TARGET_PROFILE}" \
        --output-root "${VERIFIED_STAGE}" \
        --release-lock "${WORKSPACE_ROOT}/release-lock.json" \
        --catalog "${SCRIPT_DIR}/component-catalog-bootstrap-v1.json" \
        --json > "${BUILD_WORK_DIR}/verified-service-artifacts-receipt.json"
    cp -a "${VERIFIED_STAGE}/service-artifacts/." \
        "${STAGE_DIR}/usr/share/cyrene/service-artifacts/"
    mkdir -p "${STAGE_DIR}/usr/share/cyrene/verified-service-artifacts/${TARGET_PROFILE}"
    cp -a "${VERIFIED_STAGE}/verified-published-bytes/${TARGET_PROFILE}/." \
        "${STAGE_DIR}/usr/share/cyrene/verified-service-artifacts/${TARGET_PROFILE}/"
else
    # Explicit development-only path. It is not accepted by signed release or
    # native install contract validation.
    "${RUNTIME_PYTHON}" "${SCRIPT_DIR}/service_bundle.py" build \
        --wheelhouse "${SERVICE_WHEELHOUSE}" \
        --release-lock "${WORKSPACE_ROOT}/release-lock.json" \
        --output "${STAGE_DIR}/usr/share/cyrene/service-artifacts" \
        --arch "${ARCH}" \
        --target-profile "${TARGET_PROFILE}" \
        --python-executable "${RUNTIME_PYTHON}"
fi

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
chmod 644 "${STAGE_DIR}/etc/cyrene/cyrene.env"
printf '%s\n' '/etc/cyrene/cyrene.env' > "${STAGE_DIR}/DEBIAN/conffiles"

if [[ -f "${SCRIPT_DIR}/Caddyfile.template" ]]; then
    cp "${SCRIPT_DIR}/Caddyfile.template" "${STAGE_DIR}/etc/cyrene/Caddyfile.template"
    chmod 644 "${STAGE_DIR}/etc/cyrene/Caddyfile.template"
    printf '%s\n' '/etc/cyrene/Caddyfile.template' >> "${STAGE_DIR}/DEBIAN/conffiles"
fi

# 4. Systemd service units
# cyrene-navigator.service
cat <<'EOF' > "${STAGE_DIR}/lib/systemd/system/cyrene-navigator.service"
[Unit]
Description=Cyrene Navigator Web Host
After=network.target
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service

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
ExecStart=/opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py service-run navigator
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
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service

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
ExecStart=/opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py service-run yield
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
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service

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
ExecStart=/opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py service-run reactor
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
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service

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
ExecStart=/opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py service-run exchange
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
Requires=cyrene-runtime-maintenance.service
After=cyrene-runtime-maintenance.service

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
ExecStart=/opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py service-run catalyst
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${STAGE_DIR}/lib/systemd/system/"*.service

# 5. DEBIAN/control
cat <<EOF > "${STAGE_DIR}/DEBIAN/control"
Package: cyrene
Version: ${PACKAGE_VERSION}
Section: devel
Priority: optional
Architecture: ${ARCH}
Maintainer: Cyrene Team <team@cyrene.dev>
Depends: ca-certificates, libc6 (>= ${MINIMUM_GLIBC}), libcrypt1, libgcc-s1, systemd, policykit-1, acl, zstd
Description: Cyrene Unified Local LLM Stack
 Cyrene provides a complete local LLM development and inference platform,
 including model importation (Reactor), training drafts (Yield), dataset preparation
 (Catalyst), unified API gateway (Exchange), and same-origin console (Navigator).
EOF

# 6. DEBIAN maintainer scripts
# Package configuration is stage-only: it does not start, stop, enable, or
# disable any service; initialize broker state; rotate credentials; switch an
# active Product pointer; or fetch engines from the network.
cat <<'EOF' > "${STAGE_DIR}/DEBIAN/postinst"
#!/bin/sh
set -eu

PRIVATE_PYTHON=/opt/cyrene/python/3.12.14/bin/python3.12
if [ ! -x "${PRIVATE_PYTHON}" ]; then
    echo "ERROR: packaged private CPython is missing: ${PRIVATE_PYTHON}" >&2
    exit 1
fi
"${PRIVATE_PYTHON}" -I -c 'import sys; assert sys.version_info[:3] == (3, 12, 14), sys.version'
RUNTIME_META="$(stat -c '%u:%a' /opt/cyrene/python/3.12.14)"
if [ "${RUNTIME_META}" != "0:755" ]; then
    echo "ERROR: private Python install root must be root-owned mode 755; found ${RUNTIME_META}." >&2
    exit 1
fi

# Preserve an existing account exactly. Only create the service account on a
# fresh system where it does not yet exist.
if ! id -u cyrene >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin cyrene
fi

ensure_fresh_service_directory() {
    path="$1"
    if [ -L "$path" ]; then
        echo "ERROR: $path is a symlink; refusing to follow existing runtime state." >&2
        exit 1
    elif [ ! -e "$path" ]; then
        install -d -o cyrene -g cyrene -m 750 "$path"
    elif [ ! -d "$path" ]; then
        echo "ERROR: $path exists but is not a directory." >&2
        exit 1
    fi
}
ensure_fresh_service_directory /var/lib/cyrene
ensure_fresh_service_directory /var/log/cyrene

# Provision only the dedicated broker state directory. Existing state is
# validated without repair so package upgrades cannot rewrite broker metadata.
RUNTIME_STATE_DIR=/var/lib/cyrene/runtime
if ! getent group cyrene-runtime-maintenance >/dev/null 2>&1; then
    groupadd --system cyrene-runtime-maintenance
fi
AUTHORITY_GID="$(getent group cyrene-runtime-maintenance | cut -d: -f3)"
case "${AUTHORITY_GID}" in
    ''|*[!0-9]*)
        echo "ERROR: cyrene-runtime-maintenance group has no valid numeric gid." >&2
        exit 1
        ;;
esac
if [ -L "${RUNTIME_STATE_DIR}" ]; then
    echo "ERROR: ${RUNTIME_STATE_DIR} is a symlink; refusing to follow existing runtime state." >&2
    exit 1
elif [ ! -e "${RUNTIME_STATE_DIR}" ]; then
    install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"
elif [ ! -d "${RUNTIME_STATE_DIR}" ]; then
    echo "ERROR: ${RUNTIME_STATE_DIR} exists but is not a directory." >&2
    exit 1
fi
RUNTIME_STATE_META="$(stat -c '%u:%g:%a' -- "$RUNTIME_STATE_DIR")"
if [ "${RUNTIME_STATE_META}" != "0:${AUTHORITY_GID}:2770" ]; then
    echo "ERROR: Refusing to repair existing runtime state in place (${RUNTIME_STATE_DIR}: ${RUNTIME_STATE_META})." >&2
    exit 1
fi

# This existing command validates and stages immutable releases only when
# called without --activate-missing. It preserves existing active pointers.
if ! /usr/bin/cyrene service-bootstrap; then
    echo "ERROR: could not stage the verified Product release bytes." >&2
    exit 1
fi

# Reload unit definitions so a later, explicitly approved operator action can
# use the installed files. daemon-reload does not start or change unit state.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

echo "Cyrene package initialized in stage-only mode."
echo "Product activation and runtime cutover remain deferred to the operator."
exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/postinst"

cat <<'EOF' > "${STAGE_DIR}/DEBIAN/prerm"
#!/bin/sh
set -eu
# Deliberately leave existing runtime processes and administrator unit state
# untouched during upgrade, removal, or purge.
exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/prerm"

cat <<'EOF' > "${STAGE_DIR}/DEBIAN/postrm"
#!/bin/sh
set -eu
# Removing package files never stops services or mutates runtime state.
# Refresh only systemd's file cache after a unit-file removal.
case "${1:-}" in
    remove|purge)
        if [ -d /run/systemd/system ]; then
            systemctl daemon-reload
        fi
        ;;
esac
exit 0
EOF
chmod 755 "${STAGE_DIR}/DEBIAN/postrm"

if [[ -n "${VERIFIED_SERVICE_ARTIFACTS}" ]]; then
    "${RUNTIME_PYTHON}" "${SCRIPT_DIR}/native_install_contract.py" create \
        --target-profile "${TARGET_PROFILE}" \
        --service-artifacts-index "${STAGE_DIR}/usr/share/cyrene/service-artifacts/index.json" \
        --scripts-dir "${STAGE_DIR}/DEBIAN" \
        --output "${STAGE_DIR}/usr/share/cyrene/native-install-contract-v1.json"
    chmod 644 "${STAGE_DIR}/usr/share/cyrene/native-install-contract-v1.json"
fi

# 7. Build .deb package
echo "==> Building package with dpkg-deb: ${OUTPUT_PACKAGE}..."
TEMP_PACKAGE="${TEMP_OUTPUT_DIR}/package.deb"
dpkg-deb --build --root-owner-group "${STAGE_DIR}" "${TEMP_PACKAGE}"
dpkg-deb -I "${TEMP_PACKAGE}"
mv -f -- "${TEMP_PACKAGE}" "${OUTPUT_PACKAGE}"
rmdir "${TEMP_OUTPUT_DIR}"

echo "==> Package built successfully:"
ls -lh "${OUTPUT_PACKAGE}"
