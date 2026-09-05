#!/usr/bin/env bash
set -Eeuo pipefail

# Reproducible Phase 7 vertical acceptance. The default refs are immutable
# accepted/candidate commits; callers may override them explicitly when
# validating a newer published commit. Every repository is cloned into this
# run's private Linux root, so the harness never depends on a sibling checkout
# or on JetBrains' multi-root topology.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

CYRENE_PLATFORM_URL="${CYRENE_PLATFORM_URL:-https://github.com/DoHorizon-AI/Cyrene-Platform.git}"
CYRENE_ASTRBOT_URL="${CYRENE_ASTRBOT_URL:-https://github.com/DoHorizon-AI/Astrbot-Rev.git}"
CYRENE_PLUGINS_URL="${CYRENE_PLUGINS_URL:-https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git}"

CYRENE_PLATFORM_REF="${CYRENE_PLATFORM_REF:-f46190e0f5fed19ff43d6805e5d5823ae1b77557}"
CYRENE_ASTRBOT_REF="${CYRENE_ASTRBOT_REF:-95197205611fddfd39e4127ab69ca55efdf142d9}"
CYRENE_PLUGINS_REF="${CYRENE_PLUGINS_REF:-78b47bf1ffcf3fed83e0db36507b5cbbe8574a2e}"
CYRENE_PHASE7_POSTGRES_IMAGE="${CYRENE_PHASE7_POSTGRES_IMAGE:-pgvector/pgvector@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f}"

TMP_PARENT="${CYRENE_PHASE7_TMP_PARENT:-/tmp/cyrene}"
mkdir -p -- "${TMP_PARENT}"
RUN_ROOT="$(mktemp -d "${TMP_PARENT%/}/phase7.XXXXXX")"
RUN_TMP="${RUN_ROOT}/tmp"
mkdir -p -- "${RUN_TMP}" "${RUN_ROOT}/logs" "${RUN_ROOT}/repos"
export TMPDIR="${RUN_TMP}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${RUN_ROOT}/uv-cache}"
mkdir -p -- "${UV_CACHE_DIR}"

declare -a PROCESS_PIDS=()
declare -a PROCESS_NAMES=()
declare -a TRACKED_PORTS=()
PG_CONTAINER=""
HOST_PID=""
CES_PID=""
PRIMARY_STATUS=0
CLEANUP_STATUS=0

fail() {
    echo "PHASE7_FAIL: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

wait_for_docker() {
    for _ in {1..12}; do
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

assert_linux_path() {
    local path="$1"
    case "${path}" in
        C:*|c:*|/mnt/c/*|/mnt/C/*)
            fail "Windows or DrvFs path entered the Linux vertical harness: ${path}"
            ;;
    esac
}

port_is_listening() {
    local port="$1"
    ss -Hln "sport = :${port}" 2>/dev/null | grep -q .
}

free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_for_file() {
    local path="$1"
    local seconds="$2"
    local attempts=$((seconds * 10))
    for ((attempt = 0; attempt < attempts; attempt++)); do
        if [[ -s "${path}" ]]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

start_process() {
    local name="$1"
    shift
    local log_path="${RUN_ROOT}/logs/${name}.log"
    setsid "$@" >"${log_path}" 2>&1 &
    local pid=$!
    local pgid=""
    for _ in {1..20}; do
        pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d ' ' || true)"
        [[ "${pgid}" == "${pid}" ]] && break
        sleep 0.05
    done
    if [[ "${pgid}" != "${pid}" ]]; then
        echo "process ${name} did not receive an isolated process group" >&2
        return 1
    fi
    PROCESS_PIDS+=("${pid}")
    PROCESS_NAMES+=("${name}")
    LAST_STARTED_PID="${pid}"
}

clone_exact() {
    local name="$1"
    local url="$2"
    local ref="$3"
    local destination="${RUN_ROOT}/repos/${name}"
    local cloned=0
    local auth_header=""
    if [[ -n "${CYRENE_CROSS_REPO_TOKEN:-}" && "${url}" =~ ^https://github.com/ ]]; then
        auth_header="$(printf 'x-access-token:%s' "${CYRENE_CROSS_REPO_TOKEN}" | base64 -w0)"
    fi
    for attempt in 1 2 3; do
        if [[ -e "${destination}" ]]; then
            rm -rf -- "${destination}"
        fi
        if [[ -n "${auth_header}" ]]; then
            GIT_CONFIG_COUNT=1 \
                GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
                GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${auth_header}" \
                timeout --foreground 180s git clone --no-checkout --quiet "${url}" "${destination}" && cloned=1
        elif timeout --foreground 180s git clone --no-checkout --quiet "${url}" "${destination}"; then
            cloned=1
        fi
        if [[ "${cloned}" == "1" ]]; then
            break
        fi
        if [[ "${attempt}" != "3" ]]; then
            sleep $((attempt * 2))
        fi
    done
    if [[ "${cloned}" != "1" ]]; then
        echo "::error title=BLOCKED_INFRASTRUCTURE::${name} clone failed after three attempts despite credentials being present (network timeout, service degradation, or git transport error)." >&2
        fail "BLOCKED_INFRASTRUCTURE: ${name} clone failed after three attempts"
    fi
    if [[ -n "${auth_header}" ]]; then
        GIT_CONFIG_COUNT=1 \
            GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
            GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${auth_header}" \
            git -C "${destination}" fetch --all --prune --quiet ||
            fail "BLOCKED_INFRASTRUCTURE: ${name} fetch failed after clone"
    else
        git -C "${destination}" fetch --all --prune --quiet ||
            fail "BLOCKED_INFRASTRUCTURE: ${name} fetch failed after clone"
    fi
    git -C "${destination}" cat-file -e "${ref}^{commit}" ||
        fail "BLOCKED_INFRASTRUCTURE: ${name} does not contain requested commit ${ref}"
    git -C "${destination}" checkout --quiet --detach "${ref}" ||
        fail "BLOCKED_INFRASTRUCTURE: ${name} checkout failed for ${ref}"
    local actual
    actual="$(git -C "${destination}" rev-parse HEAD)"
    [[ "${actual}" == "${ref}" ]] || fail "${name} checkout mismatch: ${actual} != ${ref}"
    assert_linux_path "$(realpath -e "${destination}")"
    git -C "${destination}" diff --exit-code
    git -C "${destination}" diff --cached --exit-code
    printf '%s\n' "${destination}"
}

cleanup() {
    PRIMARY_STATUS=$?
    trap - EXIT
    set +e

    # Stop the host first so subscriptions and CES worker sessions drain
    # before the external peers and PostgreSQL disappear.
    for ((index = ${#PROCESS_PIDS[@]} - 1; index >= 0; index--)); do
        pid="${PROCESS_PIDS[index]}"
        if kill -0 "${pid}" 2>/dev/null; then
            kill -INT -- "-${pid}" 2>/dev/null
        fi
    done
    for _ in {1..100}; do
        alive=0
        for pid in "${PROCESS_PIDS[@]}"; do
            kill -0 "${pid}" 2>/dev/null && alive=1
        done
        [[ "${alive}" == "0" ]] && break
        sleep 0.1
    done
    for pid in "${PROCESS_PIDS[@]}"; do
        if kill -0 "${pid}" 2>/dev/null; then
            kill -TERM -- "-${pid}" 2>/dev/null
        fi
    done
    for _ in {1..50}; do
        alive=0
        for pid in "${PROCESS_PIDS[@]}"; do
            kill -0 "${pid}" 2>/dev/null && alive=1
        done
        [[ "${alive}" == "0" ]] && break
        sleep 0.1
    done
    for pid in "${PROCESS_PIDS[@]}"; do
        if kill -0 "${pid}" 2>/dev/null; then
            echo "process group ${pid} did not stop; sending SIGKILL to the owned group" >&2
            kill -KILL -- "-${pid}" 2>/dev/null
        fi
        wait "${pid}" 2>/dev/null
    done

    if [[ -n "${PG_CONTAINER}" ]]; then
        if docker ps -a --format '{{.Names}}' | grep -Fxq "${PG_CONTAINER}"; then
            docker rm -f "${PG_CONTAINER}" >/dev/null 2>&1 || CLEANUP_STATUS=1
        fi
        if docker ps -a --format '{{.Names}}' | grep -Fxq "${PG_CONTAINER}"; then
            echo "owned PostgreSQL container remains: ${PG_CONTAINER}" >&2
            CLEANUP_STATUS=1
        fi
    fi

    for pid in "${PROCESS_PIDS[@]}"; do
        if kill -0 "${pid}" 2>/dev/null; then
            echo "owned process remains after cleanup: ${pid}" >&2
            CLEANUP_STATUS=1
        fi
        if pgrep -g "${pid}" >/dev/null 2>&1; then
            echo "owned process group remains after cleanup: ${pid}" >&2
            CLEANUP_STATUS=1
        fi
    done
    for port in "${TRACKED_PORTS[@]}"; do
        if port_is_listening "${port}"; then
            echo "owned port remains listening after cleanup: ${port}" >&2
            CLEANUP_STATUS=1
        fi
    done

    if [[ "${PRIMARY_STATUS}" == "0" && "${CLEANUP_STATUS}" != "0" ]]; then
        PRIMARY_STATUS=1
    fi
    if [[ "${PRIMARY_STATUS}" == "0" ]]; then
        rm -rf -- "${RUN_ROOT}"
        if [[ -e "${RUN_ROOT}" ]]; then
            echo "owned package/runtime temporary state remains: ${RUN_ROOT}" >&2
            PRIMARY_STATUS=1
        else
            echo "PHASE7_CLEANUP_PASS: child process groups, ports, PostgreSQL container, package/runtime state"
        fi
    else
        echo "PHASE7_EVIDENCE_ROOT=${RUN_ROOT}" >&2
        echo "PHASE7_CLEANUP_STATUS=${CLEANUP_STATUS}" >&2
    fi
    exit "${PRIMARY_STATUS}"
}
trap cleanup EXIT

require_command git
require_command cargo
require_command dotnet
require_command python3
require_command docker
require_command uv
require_command ss
require_command setsid
require_command pgrep
require_command timeout
require_command base64
[[ "$(uname -s)" == "Linux" ]] || fail "P0 vertical harness requires Linux"
wait_for_docker || fail "Docker daemon is unavailable after 24 seconds"

echo "PHASE7_RUN_ROOT=${RUN_ROOT}"
echo "PHASE7_PLATFORM_REF=${CYRENE_PLATFORM_REF}"
echo "PHASE7_ASTRBOT_REF=${CYRENE_ASTRBOT_REF}"
echo "PHASE7_PLUGINS_REF=${CYRENE_PLUGINS_REF}"
echo "PHASE7_POSTGRES_IMAGE=${CYRENE_PHASE7_POSTGRES_IMAGE}"

# Strict distinction between SKIPPED_CREDENTIALS and BLOCKED_INFRASTRUCTURE
if [[ -z "${CYRENE_CROSS_REPO_TOKEN:-}" ]]; then
    if [[ "${CROSS_REPO_REQUIRED:-false}" == "true" ]]; then
        echo "::error title=CROSS_REPO_REQUIRED_FAILURE::secrets.CYRENE_CROSS_REPO_TOKEN is missing for required cross-repo release/nightly gate." >&2
        fail "CROSS_REPO_REQUIRED_FAILURE: missing secrets.CYRENE_CROSS_REPO_TOKEN"
    else
        echo "::notice title=SKIPPED_CREDENTIALS::secrets.CYRENE_CROSS_REPO_TOKEN is not configured in this environment. Multi-repo vertical acceptance skipped."
        exit 0
    fi
fi

PLATFORM_ROOT="$(clone_exact platform "${CYRENE_PLATFORM_URL}" "${CYRENE_PLATFORM_REF}")"
ASTRBOT_ROOT="$(clone_exact astrbot "${CYRENE_ASTRBOT_URL}" "${CYRENE_ASTRBOT_REF}")"
PLUGINS_ROOT="$(clone_exact plugins "${CYRENE_PLUGINS_URL}" "${CYRENE_PLUGINS_REF}")"
assert_linux_path "${PLATFORM_ROOT}"
assert_linux_path "${ASTRBOT_ROOT}"
assert_linux_path "${PLUGINS_ROOT}"

CES_EXAMPLE="${PLATFORM_ROOT}/framework/crates/cy-capability-execution-service/examples/configured_binding_server.rs"
[[ -f "${CES_EXAMPLE}" ]] || fail "Platform ref does not contain configured_binding_server example"

echo "[1/9] Building, publishing, installing, and exercising the immutable OneBot package"
BOOTSTRAP_VENV="${RUN_ROOT}/package-bootstrap-venv"
uv venv --python "$(command -v python3)" "${BOOTSTRAP_VENV}"
"${BOOTSTRAP_VENV}/bin/python" -I -c \
    'import importlib.util; assert importlib.util.find_spec("onebot_v11_connector") is None' || \
    fail "minimal package host unexpectedly has OneBot preinstalled"
uv pip install --python "${BOOTSTRAP_VENV}/bin/python" \
    "protobuf==4.25.9" "pytest==8.4.2" "jsonschema==4.25.1"
PYTHONPATH="${PLUGINS_ROOT}/tools:${PLUGINS_ROOT}/plugins/connectors/onebot-v11/src" \
CYRENE_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
    "${BOOTSTRAP_VENV}/bin/python" -m pytest -q \
    "${PLUGINS_ROOT}/tools/connector_package/tests"

PACKAGE_HOST_ROOT="${RUN_ROOT}/package-host"
PACKAGE_RESULT="${RUN_ROOT}/package-install-result.json"
"${BOOTSTRAP_VENV}/bin/python" "${SCRIPT_DIR}/package_install.py" \
    --plugins-root "${PLUGINS_ROOT}" \
    --host-root "${PACKAGE_HOST_ROOT}" \
    --source-revision "${CYRENE_PLUGINS_REF}" \
    --output "${PACKAGE_RESULT}" \
    --uv "$(command -v uv)"
mapfile -t PACKAGE_PATHS < <(
    "${BOOTSTRAP_VENV}/bin/python" -c \
        'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(value["installed_root"]); print(value["manifest"]); print(value["runtime_python"])' \
        "${PACKAGE_RESULT}"
)
[[ "${#PACKAGE_PATHS[@]}" == "3" ]] || fail "package installer returned incomplete paths"
INSTALLED_PACKAGE_ROOT="${PACKAGE_PATHS[0]}"
MANIFEST="${PACKAGE_PATHS[1]}"
RUNTIME_PYTHON="${PACKAGE_PATHS[2]}"
for path in "${INSTALLED_PACKAGE_ROOT}" "${MANIFEST}" "${RUNTIME_PYTHON}"; do
    assert_linux_path "${path}"
    [[ "${path}" == "${RUN_ROOT}/"* ]] || fail "package path escaped disposable host root: ${path}"
    [[ "${path}" != "${PLUGINS_ROOT}/"* ]] || fail "package runtime fell back to source checkout: ${path}"
done
[[ -f "${MANIFEST}" ]] || fail "installed package manifest is missing"
[[ -x "${RUNTIME_PYTHON}" ]] || fail "installed package runtime is missing"

# Make the original connector source unavailable before CES starts.  The
# remaining Plugins checkout contains only the lifecycle authority used above;
# worker execution can succeed only from the verified installed package.
SOURCE_PACKAGE_ROOT="${PLUGINS_ROOT}/plugins/connectors/onebot-v11"
SOURCE_DISABLED_ROOT="${RUN_ROOT}/source-checkout-disabled"
mkdir -p -- "${SOURCE_DISABLED_ROOT}"
mv -- "${SOURCE_PACKAGE_ROOT}" "${SOURCE_DISABLED_ROOT}/onebot-v11"
[[ ! -e "${SOURCE_PACKAGE_ROOT}" ]] || fail "OneBot source checkout remains available"

echo "[2/9] Building the real Platform CES fixture"
cargo build --locked --manifest-path "${PLATFORM_ROOT}/Cargo.toml" \
    -p cy-capability-execution-service --example configured_binding_server
CES_BINARY="${PLATFORM_ROOT}/target/debug/examples/configured_binding_server"
[[ -x "${CES_BINARY}" ]] || fail "CES fixture binary was not built"

echo "[3/9] Starting two external fake OneBot peers"
MAIN_READY="${RUN_ROOT}/main-peer.ready"
SECONDARY_READY="${RUN_ROOT}/secondary-peer.ready"
MAIN_STATE="${RUN_ROOT}/main-peer.jsonl"
SECONDARY_STATE="${RUN_ROOT}/secondary-peer.jsonl"
MAIN_TOKEN="cyrene-phase7-main-token"
SECONDARY_TOKEN="cyrene-phase7-secondary-token"

start_process main-peer python3 "${SCRIPT_DIR}/fake-peer/fake_onebot_peer.py" \
    --binding-id qq-main --account-id 10001 --access-token "${MAIN_TOKEN}" \
    --state-file "${MAIN_STATE}" --ready-file "${MAIN_READY}"
MAIN_PEER_PID="${LAST_STARTED_PID}"
start_process secondary-peer python3 "${SCRIPT_DIR}/fake-peer/fake_onebot_peer.py" \
    --binding-id qq-secondary --account-id 10002 --access-token "${SECONDARY_TOKEN}" \
    --state-file "${SECONDARY_STATE}" --ready-file "${SECONDARY_READY}"
SECONDARY_PEER_PID="${LAST_STARTED_PID}"
wait_for_file "${MAIN_READY}" 10 || fail "qq-main fake peer did not become ready"
wait_for_file "${SECONDARY_READY}" 10 || fail "qq-secondary fake peer did not become ready"
MAIN_PEER_ADDRESS="$(tr -d '\r\n' < "${MAIN_READY}")"
SECONDARY_PEER_ADDRESS="$(tr -d '\r\n' < "${SECONDARY_READY}")"
MAIN_PEER_PORT="${MAIN_PEER_ADDRESS##*:}"
SECONDARY_PEER_PORT="${SECONDARY_PEER_ADDRESS##*:}"
TRACKED_PORTS+=("${MAIN_PEER_PORT}" "${SECONDARY_PEER_PORT}")

BINDINGS_FILE="${RUN_ROOT}/bindings.json"
PHASE7_MAIN_WS_URL="ws://${MAIN_PEER_ADDRESS}/" \
PHASE7_SECONDARY_WS_URL="ws://${SECONDARY_PEER_ADDRESS}/" \
PHASE7_MAIN_TOKEN="${MAIN_TOKEN}" \
PHASE7_SECONDARY_TOKEN="${SECONDARY_TOKEN}" \
PHASE7_BINDINGS_FILE="${BINDINGS_FILE}" \
python3 -c 'import json, os; path=os.environ["PHASE7_BINDINGS_FILE"]; data=[{"id":"qq-main","environment":{"CYRENE_CAPABILITY_BINDING_ID":"qq-main","CYRENE_ONEBOT_TRANSPORT_PROFILE":"forward_websocket","CYRENE_ONEBOT_WEBSOCKET_URL":os.environ["PHASE7_MAIN_WS_URL"],"CYRENE_ONEBOT_HTTP_ACCESS_TOKEN":os.environ["PHASE7_MAIN_TOKEN"],"CYRENE_ONEBOT_SELF_ACCOUNT_ID":"10001","CYRENE_ONEBOT_TIMEOUT_SECONDS":"5"}},{"id":"qq-secondary","environment":{"CYRENE_CAPABILITY_BINDING_ID":"qq-secondary","CYRENE_ONEBOT_TRANSPORT_PROFILE":"forward_websocket","CYRENE_ONEBOT_WEBSOCKET_URL":os.environ["PHASE7_SECONDARY_WS_URL"],"CYRENE_ONEBOT_HTTP_ACCESS_TOKEN":os.environ["PHASE7_SECONDARY_TOKEN"],"CYRENE_ONEBOT_SELF_ACCOUNT_ID":"10002","CYRENE_ONEBOT_TIMEOUT_SECONDS":"5"}}]; json.dump(data, open(path,"w",encoding="utf-8"), indent=2); open(path,"a",encoding="utf-8").write("\n")'

echo "[4/9] Starting the real Platform CES with two package-installed bindings"
CES_READY="${RUN_ROOT}/ces.ready"
start_process capability-execution-service "${CES_BINARY}" \
    --manifest "${MANIFEST}" --bindings "${BINDINGS_FILE}" \
    --bind 127.0.0.1:0 --ready-file "${CES_READY}" \
    --working-dir "${INSTALLED_PACKAGE_ROOT}" \
    --python-path "${INSTALLED_PACKAGE_ROOT}/src" \
    --python-path "${PLATFORM_ROOT}/sdk/python" \
    --python-executable "${RUNTIME_PYTHON}" \
    --handshake-timeout-ms 5000 --default-invoke-timeout-ms 15000 \
    --shutdown-grace-ms 2000 --event-buffer-capacity 32
CES_PID="${LAST_STARTED_PID}"
wait_for_file "${CES_READY}" 20 || fail "Platform CES did not become ready"
CES_ADDRESS="$(tr -d '\r\n' < "${CES_READY}")"
CES_PORT="${CES_ADDRESS##*:}"
CES_ENDPOINT="http://${CES_ADDRESS}"
TRACKED_PORTS+=("${CES_PORT}")

echo "[5/9] Starting disposable PostgreSQL/pgvector and applying AstrBot migrations"
PG_CONTAINER="cyrene-phase7-pg-${RUN_ROOT##*.}"
if docker ps -a --format '{{.Names}}' | grep -Fxq "${PG_CONTAINER}"; then
    fail "owned PostgreSQL container name already exists: ${PG_CONTAINER}"
fi
PG_PORT="$(free_port)"
TRACKED_PORTS+=("${PG_PORT}")
DB_NAME="cyrene_phase7"
MIGRATOR_USER="cyrene_migrator"
APP_USER="cyrene_app"
MIGRATOR_PASSWORD="migrate_${RUN_ROOT##*.}"
APP_PASSWORD="app_${RUN_ROOT##*.}"
docker run --rm -d --name "${PG_CONTAINER}" \
    -e POSTGRES_PASSWORD="postgres_phase7" \
    -p "127.0.0.1:${PG_PORT}:5432" \
    "${CYRENE_PHASE7_POSTGRES_IMAGE}" >/dev/null
for _ in {1..120}; do
    if docker exec "${PG_CONTAINER}" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done
docker exec "${PG_CONTAINER}" pg_isready -U postgres -d postgres >/dev/null
docker exec "${PG_CONTAINER}" psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c "CREATE ROLE \"${MIGRATOR_USER}\" LOGIN PASSWORD '${MIGRATOR_PASSWORD}'"
docker exec "${PG_CONTAINER}" psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c "CREATE ROLE \"${APP_USER}\" LOGIN PASSWORD '${APP_PASSWORD}'"
docker exec "${PG_CONTAINER}" psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
    -c "CREATE DATABASE \"${DB_NAME}\" OWNER \"${MIGRATOR_USER}\""
docker exec "${PG_CONTAINER}" psql -v ON_ERROR_STOP=1 -U postgres -d "${DB_NAME}" \
    -c "CREATE EXTENSION IF NOT EXISTS vector"
MIGRATOR_CONNECTION="Host=127.0.0.1;Port=${PG_PORT};Database=${DB_NAME};Username=${MIGRATOR_USER};Password=${MIGRATOR_PASSWORD};Timeout=10;Command Timeout=300"
APP_CONNECTION="Host=127.0.0.1;Port=${PG_PORT};Database=${DB_NAME};Username=${APP_USER};Password=${APP_PASSWORD};Timeout=10;Command Timeout=60"
MIGRATOR_PROJECT="${ASTRBOT_ROOT}/src/AstrBot.DatabaseMigrator/AstrBot.DatabaseMigrator.csproj"
dotnet build "${MIGRATOR_PROJECT}" --configuration Release --property:NuGetAudit=false \
    --property:BaseOutputPath="${RUN_ROOT}/migrator-out/" \
    --property:BaseIntermediateOutputPath="${RUN_ROOT}/migrator-obj/"
MIGRATOR_DLL="$(find "${RUN_ROOT}/migrator-out" -type f -name AstrBot.DatabaseMigrator.dll -print -quit)"
[[ -n "${MIGRATOR_DLL}" ]] || fail "database migrator binary was not built"
ConnectionStrings__AstrBotMigrator="${MIGRATOR_CONNECTION}" \
ConnectionStrings__AstrBot="${APP_CONNECTION}" \
ASTRBOT_SKIP_DATABASE_ROLE_PERMISSION_VALIDATION=true \
    dotnet "${MIGRATOR_DLL}"
docker exec "${PG_CONTAINER}" psql -v ON_ERROR_STOP=1 -U postgres -d "${DB_NAME}" \
    -c "GRANT USAGE ON SCHEMA public TO \"${APP_USER}\"; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO \"${APP_USER}\"; GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO \"${APP_USER}\""

echo "[6/9] Building and starting the real AstrBot test host"
HOST_DRIVER="${WORKSPACE_ROOT}/ci/vertical-e2e/host-driver/Cyrene.Phase7.HostDriver.csproj"
HOST_OUTPUT_ROOT="${ASTRBOT_ROOT}/.phase7-host-out"
DOTNET_PROPS=(
    "-p:AstrBotRoot=${ASTRBOT_ROOT}"
    "-p:BaseOutputPath=${HOST_OUTPUT_ROOT}/"
    "-p:NuGetAudit=false"
)
dotnet restore "${HOST_DRIVER}" "${DOTNET_PROPS[@]}"
dotnet build "${HOST_DRIVER}" --no-restore --configuration Release "${DOTNET_PROPS[@]}"
HOST_DLL="$(find "${HOST_OUTPUT_ROOT}" -type f -name Cyrene.Phase7.HostDriver.dll -print -quit)"
[[ -n "${HOST_DLL}" ]] || fail "AstrBot host driver binary was not built"
DATA_ROOT="${RUN_ROOT}/astrbot-data"
mkdir -p -- "${DATA_ROOT}"
export CYRENE_PHASE7_ASTRBOT_ROOT="${ASTRBOT_ROOT}"
export CYRENE_PHASE7_DATA_ROOT="${DATA_ROOT}"
export CYRENE_PHASE7_DATABASE="${APP_CONNECTION}"
export CYRENE_PHASE7_CES_ENDPOINT="${CES_ENDPOINT}"
export CYRENE_PHASE7_MAIN_STATE="${MAIN_STATE}"
export CYRENE_PHASE7_SECONDARY_STATE="${SECONDARY_STATE}"
start_process astrbot-test-host dotnet "${HOST_DLL}"
HOST_PID="${LAST_STARTED_PID}"

echo "[7/9] Awaiting the installed package -> CES -> AstrBot -> PostgreSQL round trip"
if wait "${HOST_PID}"; then
    HOST_EXIT=0
else
    HOST_EXIT=$?
fi
[[ "${HOST_EXIT}" == "0" ]] || fail "AstrBot Phase 7 host driver exited with ${HOST_EXIT}"

echo "[8/9] Verifying package-installed execution evidence"
"${BOOTSTRAP_VENV}/bin/python" -c \
    'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")); assert value["offline_reinstall"] == "PASS"; assert value["corrupt_cache_rejection"] == "PASS"; assert value["upgrade_rollback"] == "PASS"; assert {item["binding_id"] for item in value["bindings"]} == {"qq-main", "qq-secondary"}' \
    "${PACKAGE_RESULT}"
[[ ! -e "${SOURCE_PACKAGE_ROOT}" ]] || fail "source-tree fallback reappeared during worker execution"
echo "PACKAGE_INSTALLED_VERTICAL_PASS"

echo "[9/9] Phase 7 assertions passed; cleanup is verified by the EXIT trap"
echo "PHASE7_VERTICAL_PASS"
