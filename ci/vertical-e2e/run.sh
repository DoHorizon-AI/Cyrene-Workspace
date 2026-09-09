#!/usr/bin/env bash
# ┌─────────────────────────────────────────────────────────────────────┐
# │  📄 ci/vertical-e2e/run.sh                                          │
# │  Role: Proves the exact-ref Product-to-Plugin data-plane boundary.  │
# │                                                                     │
# │  脚本职责：验证精确提交下 Product 直连 Plugin，Platform 仅负责通用运行。 │
# └─────────────────────────────────────────────────────────────────────┘
set -Eeuo pipefail

CYRENE_PLATFORM_URL="${CYRENE_PLATFORM_URL:-https://github.com/DoHorizon-AI/Cyrene-Platform.git}"
CYRENE_PLUGINS_URL="${CYRENE_PLUGINS_URL:-https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git}"
CYRENE_ASTRBOT_URL="${CYRENE_ASTRBOT_URL:-https://github.com/DoHorizon-AI/Astrbot-Rev.git}"

CYRENE_PLATFORM_REF="${CYRENE_PLATFORM_REF:-2b9538230f92d6129bd97126c478ee70008cf86f}"
CYRENE_PLUGINS_REF="${CYRENE_PLUGINS_REF:-a019cc37cab39ffafe30884a4a0427aaaadf69a9}"
CYRENE_ASTRBOT_REF="${CYRENE_ASTRBOT_REF:-30a4d7c40629691c809e6d0609532caa8001b498}"

TMP_PARENT="${CYRENE_DIRECT_E2E_TMP_PARENT:-/tmp/cyrene}"
mkdir -p -- "${TMP_PARENT}"
RUN_ROOT="$(mktemp -d "${TMP_PARENT%/}/direct-plugin.XXXXXX")"
PRIMARY_STATUS=0

# Stop immediately with a stable acceptance prefix.
fail() {
    echo "DIRECT_PLUGIN_E2E_FAIL: $*" >&2
    exit 1
}

# Require every tool used by the cross-repository acceptance.
require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

# Remove only the disposable run root created by this script.
remove_tree() {
    local path="$1"
    python3 - "${path}" <<'PY'
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1])
if target.exists():
    shutil.rmtree(target)
PY
}

# Preserve failure evidence and remove successful temporary state.
cleanup() {
    PRIMARY_STATUS=$?
    trap - EXIT
    if [[ "${PRIMARY_STATUS}" == "0" ]]; then
        remove_tree "${RUN_ROOT}"
        echo "DIRECT_PLUGIN_E2E_CLEANUP_PASS"
    else
        echo "DIRECT_PLUGIN_E2E_EVIDENCE_ROOT=${RUN_ROOT}" >&2
    fi
    exit "${PRIMARY_STATUS}"
}
trap cleanup EXIT

# Verify that a supplied checkout matches the immutable candidate revision.
assert_exact_checkout() {
    local name="$1"
    local root="$2"
    local expected_ref="$3"
    local actual_ref

    [[ -d "${root}/.git" || -f "${root}/.git" ]] ||
        fail "${name} root is not a Git checkout: ${root}"
    actual_ref="$(git -C "${root}" rev-parse HEAD)"
    [[ "${actual_ref}" == "${expected_ref}" ]] ||
        fail "${name} checkout mismatch: ${actual_ref} != ${expected_ref}"
    git -C "${root}" diff --exit-code
    git -C "${root}" diff --cached --exit-code
}

# Clone one repository without persisting the optional credential.
clone_exact() {
    local name="$1"
    local url="$2"
    local expected_ref="$3"
    local destination="${RUN_ROOT}/repos/${name}"
    local auth_header=""

    mkdir -p -- "${RUN_ROOT}/repos"
    if [[ -n "${CYRENE_CROSS_REPO_TOKEN:-}" && "${url}" =~ ^https://github.com/ ]]; then
        auth_header="$(printf 'x-access-token:%s' "${CYRENE_CROSS_REPO_TOKEN}" | base64 -w0)"
        GIT_CONFIG_COUNT=1 \
            GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
            GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${auth_header}" \
            git clone --no-checkout --quiet "${url}" "${destination}"
        GIT_CONFIG_COUNT=1 \
            GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
            GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${auth_header}" \
            git -C "${destination}" fetch --quiet origin "${expected_ref}"
    else
        git clone --no-checkout --quiet "${url}" "${destination}"
        git -C "${destination}" fetch --quiet origin "${expected_ref}"
    fi
    git -C "${destination}" checkout --quiet --detach "${expected_ref}"
    assert_exact_checkout "${name}" "${destination}" "${expected_ref}"
    printf '%s\n' "${destination}"
}

# Prefer an Azure multi-checkout root and clone only for standalone runs.
resolve_checkout() {
    local name="$1"
    local supplied_root="$2"
    local url="$3"
    local expected_ref="$4"
    local resolved_root

    if [[ -n "${supplied_root}" ]]; then
        resolved_root="$(realpath -e "${supplied_root}")"
        assert_exact_checkout "${name}" "${resolved_root}" "${expected_ref}"
    else
        resolved_root="$(clone_exact "${name}" "${url}" "${expected_ref}")"
    fi
    printf '%s\n' "${resolved_root}"
}

require_command base64
require_command cargo
require_command dotnet
require_command git
require_command python3
require_command realpath
require_command uv
[[ "$(uname -s)" == "Linux" ]] || fail "the direct Plugin acceptance requires Linux"

PLATFORM_ROOT="$(resolve_checkout platform "${CYRENE_PLATFORM_ROOT:-}" "${CYRENE_PLATFORM_URL}" "${CYRENE_PLATFORM_REF}")"
PLUGINS_ROOT="$(resolve_checkout plugins "${CYRENE_OFFICIAL_PLUGINS_ROOT:-}" "${CYRENE_PLUGINS_URL}" "${CYRENE_PLUGINS_REF}")"
ASTRBOT_ROOT="$(resolve_checkout astrbot "${CYRENE_ASTRBOT_ROOT:-}" "${CYRENE_ASTRBOT_URL}" "${CYRENE_ASTRBOT_REF}")"

echo "DIRECT_PLUGIN_PLATFORM_REF=${CYRENE_PLATFORM_REF}"
echo "DIRECT_PLUGIN_PLUGINS_REF=${CYRENE_PLUGINS_REF}"
echo "DIRECT_PLUGIN_ASTRBOT_REF=${CYRENE_ASTRBOT_REF}"

# Platform contributes only the generic package lifecycle binaries. The
# capability archive, direct client, protocol peers, and assertions stay with
# their Plugins and Product owners.
PACKAGE_ROOT="${RUN_ROOT}/onebot-packages"
WHEELHOUSE="${RUN_ROOT}/onebot-wheelhouse"
mkdir -p -- "${PACKAGE_ROOT}/v1" "${PACKAGE_ROOT}/v2" "${WHEELHOUSE}"

echo "[1/4] Build the Plugins-owned immutable OneBot packages"
(
    cd "${PLUGINS_ROOT}"
    uv sync --frozen
    uv run python -m tools.connector_package \
        --source plugins/connectors/onebot-v11 \
        --output "${PACKAGE_ROOT}/v1" \
        --source-revision "${CYRENE_PLUGINS_REF}"
    uv run python -m tools.connector_package \
        --source plugins/connectors/onebot-v11 \
        --output "${PACKAGE_ROOT}/v2" \
        --source-revision "${CYRENE_PLUGINS_REF}" \
        --version 0.2.0
)

echo "[2/4] Prepare the locked offline Plugin dependency wheelhouse"
python3 -m pip download \
    --dest "${WHEELHOUSE}" \
    grpcio==1.62.3 protobuf==4.25.9

echo "[3/4] Build the generic Platform package control plane"
cargo build --locked \
    --manifest-path "${PLATFORM_ROOT}/Cargo.toml" \
    --bin cyrene-capability-resolver \
    --bin cy-package-runtime

echo "[4/4] Run the Product-owned direct OneBot lifecycle TCK"
TEST_PROJECT="${ASTRBOT_ROOT}/tests/AstrBot.DotNetHost.Tests/AstrBot.DotNetHost.Tests.csproj"
(
    cd "${ASTRBOT_ROOT}"
    bash scripts/dotnet_restore_with_fallback.sh "${TEST_PROJECT}"
    dotnet build "${TEST_PROJECT}" \
        --configuration Release \
        --no-restore \
        -p:NuGetAudit=false
    CYRENE_PLATFORM_ROOT="${PLATFORM_ROOT}" \
    CYRENE_OFFICIAL_PLUGINS_ROOT="${PLUGINS_ROOT}" \
    CYRENE_PLATFORM_PACKAGE_RUNTIME="${PLATFORM_ROOT}/target/debug/cy-package-runtime" \
    CYRENE_OFFICIAL_ONEBOT_ARCHIVE="${PACKAGE_ROOT}/v1/cyrene.connectors.onebot-v11-0.1.0.zip" \
    CYRENE_OFFICIAL_ONEBOT_DESCRIPTOR="${PACKAGE_ROOT}/v1/cyrene.connectors.onebot-v11-0.1.0.descriptor.json" \
    CYRENE_OFFICIAL_ONEBOT_UPGRADE_ARCHIVE="${PACKAGE_ROOT}/v2/cyrene.connectors.onebot-v11-0.2.0.zip" \
    CYRENE_OFFICIAL_ONEBOT_UPGRADE_DESCRIPTOR="${PACKAGE_ROOT}/v2/cyrene.connectors.onebot-v11-0.2.0.descriptor.json" \
    CYRENE_OFFICIAL_ONEBOT_WHEELHOUSE="${WHEELHOUSE}" \
    CYRENE_UV_EXECUTABLE="$(command -v uv)" \
        dotnet test "${TEST_PROJECT}" \
            --configuration Release \
            --no-build \
            --no-restore \
            --filter "FullyQualifiedName~OfficialOneBotProductRuntimeTck" \
            -p:NuGetAudit=false
)

echo "DIRECT_PRODUCT_TO_PLUGIN_VERTICAL_PASS"
