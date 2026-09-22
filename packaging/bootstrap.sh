#!/usr/bin/env bash
# Install the training and serving engines pinned by release-lock.json into an
# isolated virtualenv. Never installs an unpinned version: the RC gates on the
# exact engines that real acceptance covered, not the newest release.
#
# Usage: INSTALL_ROOT=/usr/lib/cyrene ./bootstrap.sh
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/usr/lib/cyrene}"
LOCK_FILE="${LOCK_FILE:-${INSTALL_ROOT}/release-lock.json}"
VENV_DIR="${VENV_DIR:-${INSTALL_ROOT}/runtime-venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

log() { echo "[cyrene-bootstrap] $*"; }
fail() { echo "[cyrene-bootstrap] ERROR: $*" >&2; exit 1; }

# The lock stores either an exact version ("0.25.1") or an already-complete
# specifier (">=5.5.3"). Prefixing "==" onto the latter produces an invalid
# requirement, so pass ranges through untouched.
requirement() {
    local name="$1" version="$2"
    [[ -n "${version}" ]] || return 0
    case "${version}" in
        [0-9]*) printf '%s' "${name}==${version}" ;;
        *) printf '%s' "${name}${version//[[:space:]]/}" ;;
    esac
}

[[ -f "${LOCK_FILE}" ]] || fail "release-lock.json missing at ${LOCK_FILE}; refusing to guess engine versions."
command -v uv >/dev/null 2>&1 || fail "uv is required to create the runtime environment; install uv first."
[[ -x "${INSTALL_ROOT}/scripts/cyrene" || -L /usr/bin/cyrene ]] || log "warning: cyrene CLI was not found; continuing."

# Read the pinned versions straight from the release lock.
mapfile -t versions < <(LOCK_FILE="${LOCK_FILE}" python3 - <<'PY'
import json
import os
import shlex

with open(os.environ["LOCK_FILE"], encoding="utf-8") as handle:
    lock = json.load(handle)

engines = lock.get("engines", {})
execution = engines.get("execution.engine.v1", {})
training = engines.get("training.llama-factory.v1", {})
dependencies = execution.get("resolvedDependencies", {})

for value in (
    execution.get("package", ""),
    execution.get("acceptedVersion", ""),
    training.get("package", ""),
    training.get("acceptedVersion", ""),
    dependencies.get("torch", ""),
    dependencies.get("transformers", ""),
):
    print(shlex.quote(str(value)))
PY
)

VLLM_PACKAGE="${versions[0]}"
VLLM_VERSION="${versions[1]}"
TRAINING_PACKAGE="${versions[2]}"
TRAINING_VERSION="${versions[3]}"
TORCH_VERSION="${versions[4]}"
TRANSFORMERS_VERSION="${versions[5]}"

[[ -n "${VLLM_PACKAGE}" && -n "${VLLM_VERSION}" ]] || fail "release-lock.json has no pinned serving engine."
[[ -n "${TRAINING_PACKAGE}" && -n "${TRAINING_VERSION}" ]] || fail "release-lock.json has no pinned training engine."

packages=()
[[ -n "${TORCH_VERSION}" ]] && packages+=("$(requirement torch "${TORCH_VERSION}")")
[[ -n "${TRANSFORMERS_VERSION}" ]] && packages+=("$(requirement transformers "${TRANSFORMERS_VERSION}")")
packages+=("$(requirement "${VLLM_PACKAGE}" "${VLLM_VERSION}")")
packages+=("$(requirement "${TRAINING_PACKAGE}" "${TRAINING_VERSION}")")

log "Creating runtime virtualenv at ${VENV_DIR} (python ${PYTHON_VERSION})"
uv venv --python "${PYTHON_VERSION}" "${VENV_DIR}"

log "Installing pinned engines: ${packages[*]}"
# Torch carries its own CUDA wheels, so use its index rather than PyPI defaults.
if [[ -n "${TORCH_VERSION}" ]]; then
    uv pip install --python "${VENV_DIR}/bin/python" \
        --index-url https://download.pytorch.org/whl/cu124 \
        "torch==${TORCH_VERSION}"
fi
uv pip install --python "${VENV_DIR}/bin/python" "${packages[@]}"

if [[ -d /var/lib/cyrene ]]; then
    chown -R cyrene:cyrene /var/lib/cyrene /var/log/cyrene 2>/dev/null || true
fi

log "Verifying that both engines import from the installed environment..."
"${VENV_DIR}/bin/python" - <<PY || fail "engine import verification failed"
import importlib.util as util
for module in ("${VLLM_PACKAGE}", "${TRAINING_PACKAGE}"):
    if util.find_spec(module) is None:
        raise SystemExit(f"{module} is not importable after installation")
    print(f"  ok: {module}")
PY

log "Done. Engines installed into ${VENV_DIR}"
log "To use them: ${VENV_DIR}/bin/python -m pip show ${VLLM_PACKAGE}"
