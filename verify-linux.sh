#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "=================================================="
echo " CYRENE CANONICAL LINUX ACCEPTANCE GATE"
echo "=================================================="

# Check operating system
OS_NAME="$(uname -s)"
if [ "${OS_NAME}" != "Linux" ]; then
    echo "ERROR: Canonical Rust runtime acceptance must be executed in a Linux environment."
    echo "Current OS: ${OS_NAME}"
    exit 1
fi

echo -e "\n[1/4] Checking Rust toolchain..."
rustc --version
cargo --version

echo -e "\n[2/4] Validating Cyrene-Platform (Rust Workspace)..."
cd "${SCRIPT_DIR}/../Cyrene-Platform"
cargo metadata --format-version 1 --no-deps >/dev/null
cargo fmt --all -- --check
cargo check --workspace --locked --all-targets
cargo test --workspace --locked

echo -e "\n[3/4] Validating Cyrene-Reactor (Rust Component)..."
cd "${SCRIPT_DIR}/../Cyrene-Services/Cyrene-Reactor"
cargo metadata --format-version 1 --no-deps >/dev/null
cargo check --workspace --locked

echo -e "\n[4/4] Validating Python Runtimes (Linux)..."
cd "${SCRIPT_DIR}/../Cyrene-Platform"
uv run pytest tooling/ci
cd "${SCRIPT_DIR}/../Cyrene-Plugins-Official"
uv run pytest conformance/tests
cd "${SCRIPT_DIR}/../Cyrene-Services/Cyrene-Reactor"
uv run pytest
cd "${SCRIPT_DIR}/../Cyrene-Services/Cyrene-Yield"
uv run pytest
cd "${SCRIPT_DIR}/../Cyrene-Services/Cyrene-Exchange"
uv run pytest

echo -e "\n=================================================="
echo " CANONICAL LINUX ACCEPTANCE: ALL GATES PASSED"
echo "=================================================="
