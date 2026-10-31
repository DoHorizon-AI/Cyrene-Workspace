"""Check the Workspace Authority Docker recipe's cross-build and cache contract."""

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile.workspace-authority"
TARGET = "aarch64-unknown-linux-gnu"


def test_authority_builder_runs_on_build_platform_and_targets_arm64() -> None:
    """Keep Rust and build tools native while emitting ARM64 executables."""

    recipe = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM --platform=$BUILDPLATFORM ${RUST_BASE_IMAGE} AS build" in recipe
    assert "ARG TARGETARCH" in recipe
    assert 'test "${TARGETARCH}" = "arm64"' in recipe
    assert "rustup target add --toolchain 1.96.1 aarch64-unknown-linux-gnu" in recipe
    assert f"--target {TARGET}" in recipe
    assert "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc" in recipe
    assert "CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc" in recipe
    assert "CXX_aarch64_unknown_linux_gnu=aarch64-linux-gnu-g++" in recipe
    assert "gcc-aarch64-linux-gnu g++-aarch64-linux-gnu" in recipe


def test_authority_cargo_uses_arch_scoped_buildkit_caches() -> None:
    """Persist registry, git, and target artifacts across source-only rebuilds."""

    recipe = DOCKERFILE.read_text(encoding="utf-8")
    mounts = [line.strip() for line in recipe.splitlines() if "--mount=type=cache" in line]
    assert len(mounts) == 3
    assert any(
        "id=cyrene-workspace-authority-registry-${TARGETARCH}" in line
        and "target=/usr/local/cargo/registry" in line
        and "sharing=locked" in line
        for line in mounts
    )
    assert any(
        "id=cyrene-workspace-authority-git-${TARGETARCH}" in line
        and "target=/usr/local/cargo/git" in line
        and "sharing=locked" in line
        for line in mounts
    )
    assert any(
        "id=cyrene-workspace-authority-target-${TARGETARCH}-rust-1.96.1" in line
        and "target=/source/target" in line
        and "sharing=locked" in line
        for line in mounts
    )
    assert "/source/target/aarch64-unknown-linux-gnu/release/" in recipe
    assert (
        "cp /source/target/aarch64-unknown-linux-gnu/release/cy-workspace-connector-authority-host"
        in recipe
    )
    assert "/source/target/aarch64-unknown-linux-gnu/release/cy-workspace-directory-admin" in recipe


def test_authority_runtime_keeps_existing_binaries_and_security_contract() -> None:
    """Preserve runtime identity, labels, secret entrypoint, and the two executable names."""

    recipe = DOCKERFILE.read_text(encoding="utf-8")
    runtime = recipe.split("FROM ${RUNTIME_BASE_IMAGE} AS runtime", maxsplit=1)[1]
    assert (
        "/build-output/cy-workspace-connector-authority-host /usr/local/bin/cy-workspace-connector-authority-host"
        in runtime
    )
    assert (
        "/build-output/cy-workspace-directory-admin /usr/local/bin/cy-workspace-directory-admin"
        in runtime
    )
    assert (
        "COPY Cyrene-Workspace/tooling/deploy/teams-cloud/secret-env-entrypoint.sh /usr/local/bin/cyrene-secret-env-entrypoint"
        in runtime
    )
    assert (
        "LABEL org.opencontainers.image.source=${SOURCE_REPOSITORY} org.opencontainers.image.revision=${SOURCE_REVISION}"
        in runtime
    )
    assert "USER 10001:10001" in runtime
    assert 'ENTRYPOINT ["/usr/local/bin/cy-workspace-connector-authority-host"]' in runtime
