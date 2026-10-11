"""Focused tests for the stage-only DEB contract evidence marker."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TARGET = "linux-ubuntu-22.04-x86_64-python-3.12"
SERVICES = (
    "cyrene-catalyst",
    "cyrene-exchange",
    "cyrene-navigator",
    "cyrene-reactor",
    "cyrene-yield",
)


def _module() -> ModuleType:
    path = WORKSPACE_ROOT / "packaging" / "native_install_contract.py"
    spec = importlib.util.spec_from_file_location("cyrene_native_install_contract_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index() -> dict:
    records = {}
    for index, component_id in enumerate(SERVICES):
        commit = f"{index + 1:x}" * 40
        records[component_id] = {
            "componentId": component_id,
            "repository": f"DoHorizon-AI/Cyrene-{component_id.removeprefix('cyrene-').title()}",
            "releaseId": f"stable-{commit}",
            "source": {"ref": "refs/heads/main", "commit": commit},
            "artifact": {
                "path": f"{component_id}.tar.gz",
                "sha256": f"{index + 1:x}" * 64,
                "sizeBytes": index + 10,
                "kind": "python-bundle",
                "format": "tar.gz",
            },
            "manifest": {
                "path": f"{component_id}.manifest.json",
                "sha256": f"{index + 2:x}" * 64,
                "manifestDigest": "sha256:" + f"{index + 3:x}" * 64,
            },
            "attestation": {
                "path": f"{component_id}.tar.gz.attestation.jsonl",
                "sha256": f"{index + 4:x}" * 64,
                "repository": f"DoHorizon-AI/Cyrene-{component_id.removeprefix('cyrene-').title()}",
                "workflow": ".github/workflows/component-release.yml",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subjectName": f"{component_id}.tar.gz",
                "sourceRef": "refs/heads/main",
                "sourceCommit": commit,
            },
        }
    return {"schemaVersion": 1, "targetProfile": TARGET, "services": records}


def _scripts(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("postinst", "prerm", "postrm"):
        (directory / name).write_text(f"#!/bin/sh\n# {name}\nexit 0\n", encoding="utf-8")


def _units(directory: Path, *, invalid_service: str | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for component_id in SERVICES:
        service = component_id.removeprefix("cyrene-")
        command = (
            "/usr/bin/cyrene component-run " + component_id
            if service == invalid_service
            else "/opt/cyrene/python/3.12.14/bin/python3.12 -sE "
            f"/usr/lib/cyrene/scripts/cyrene.py service-run {service}"
        )
        path = directory / f"{component_id}.service"
        path.write_text(
            "[Service]\nUser=cyrene\nGroup=cyrene\nExecStart=" + command + "\n",
            encoding="utf-8",
        )
        path.chmod(0o644)


def test_contract_binds_the_exact_index_bytes_and_maintainer_scripts(tmp_path: Path) -> None:
    module = _module()
    index_path = tmp_path / "service-artifacts" / "index.json"
    index_path.parent.mkdir()
    index_bytes = (json.dumps(_index(), indent=2) + "\n").encode()
    index_path.write_bytes(index_bytes)
    scripts = tmp_path / "DEBIAN"
    _scripts(scripts)
    units = tmp_path / "lib/systemd/system"
    _units(units)
    output = tmp_path / "contract.json"

    result = module.create_contract(
        target_profile=TARGET,
        service_artifacts_index=index_path,
        scripts_dir=scripts,
        unit_directory=units,
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        output=output,
    )
    written = json.loads(output.read_text(encoding="utf-8"))

    assert written == result
    assert written["initializationMode"] == "stage-only"
    assert written["serviceArtifactsMode"] == "verified-published-bytes"
    assert written["serviceActivation"] == "deferred"
    assert written["brokerAction"] == "preserve-existing"
    assert written["oldRuntimeAction"] == "preserve"
    assert written["serviceArtifactsIndexSha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert set(written["services"]) == set(SERVICES)
    assert set(written["managedUnits"]) == set(SERVICES)
    for component_id, unit in written["managedUnits"].items():
        service = component_id.removeprefix("cyrene-")
        assert unit == {
            "componentId": component_id,
            "service": service,
            "unit": f"{component_id}.service",
            "packagePath": f"lib/systemd/system/{component_id}.service",
            "sha256": hashlib.sha256((units / f"{component_id}.service").read_bytes()).hexdigest(),
            "targetProfile": TARGET,
            "source": {
                "repository": "DoHorizon-AI/Cyrene-Workspace",
                "ref": "refs/heads/develop",
                "commit": "a" * 40,
            },
        }
    assert set(written["maintainerScriptsSha256"]) == {"postinst", "prerm", "postrm"}
    for name in written["maintainerScriptsSha256"]:
        assert (
            written["maintainerScriptsSha256"][name]
            == hashlib.sha256((scripts / name).read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("corruption", ["missing-service", "source-mismatch", "wrong-profile"])
def test_contract_rejects_incomplete_or_mismatched_source_index(
    tmp_path: Path, corruption: str
) -> None:
    module = _module()
    index = _index()
    if corruption == "missing-service":
        del index["services"][SERVICES[-1]]
    elif corruption == "source-mismatch":
        index["services"][SERVICES[0]]["attestation"]["sourceCommit"] = "f" * 40
    else:
        index["targetProfile"] = "linux-ubuntu-24.04-x86_64-python-3.12"
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    scripts = tmp_path / "DEBIAN"
    _scripts(scripts)
    units = tmp_path / "lib/systemd/system"
    _units(units)

    with pytest.raises(module.ContractError):
        module.create_contract(
            target_profile=TARGET,
            service_artifacts_index=index_path,
            scripts_dir=scripts,
            unit_directory=units,
            source_ref="refs/heads/develop",
            source_commit="a" * 40,
            output=tmp_path / "contract.json",
        )


@pytest.mark.parametrize(
    "corruption",
    ["wrong-runner", "invalid-target", "invalid-source"],
)
def test_contract_rejects_unmanaged_runner_or_source_target_tuple(
    tmp_path: Path, corruption: str
) -> None:
    module = _module()
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(_index()), encoding="utf-8")
    scripts = tmp_path / "DEBIAN"
    _scripts(scripts)
    units = tmp_path / "lib/systemd/system"
    _units(units, invalid_service="catalyst" if corruption == "wrong-runner" else None)

    arguments = {
        "target_profile": TARGET,
        "service_artifacts_index": index_path,
        "scripts_dir": scripts,
        "unit_directory": units,
        "source_ref": "refs/heads/develop",
        "source_commit": "a" * 40,
        "output": tmp_path / "contract.json",
    }
    if corruption == "invalid-target":
        arguments["target_profile"] = "linux-ubuntu-20.04-x86_64-python-3.12"
    elif corruption == "invalid-source":
        arguments["source_commit"] = "not-a-commit"

    with pytest.raises(module.ContractError):
        module.create_contract(**arguments)


def test_verified_release_builder_scripts_remain_stage_only() -> None:
    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    postinst = source.split("cat <<'EOF' > \"${STAGE_DIR}/DEBIAN/postinst\"\n", 1)[1].split(
        '\nEOF\nchmod 755 "${STAGE_DIR}/DEBIAN/postinst"', 1
    )[0]
    prerm = source.split("cat <<'EOF' > \"${STAGE_DIR}/DEBIAN/prerm\"\n", 1)[1].split(
        '\nEOF\nchmod 755 "${STAGE_DIR}/DEBIAN/prerm"', 1
    )[0]
    postrm = source.split("cat <<'EOF' > \"${STAGE_DIR}/DEBIAN/postrm\"\n", 1)[1].split(
        '\nEOF\nchmod 755 "${STAGE_DIR}/DEBIAN/postrm"', 1
    )[0]
    maintainer_scripts = "\n".join(
        "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        for script in (postinst, prerm, postrm)
    )

    assert "/usr/bin/cyrene service-bootstrap;" in postinst
    assert "--activate-missing" not in maintainer_scripts
    assert "bootstrap.sh" not in maintainer_scripts
    assert "/usr/bin/python3.12" not in maintainer_scripts
    assert "chown" not in maintainer_scripts
    assert "init-catalog" not in maintainer_scripts
    assert "cyrene init" not in maintainer_scripts
    for forbidden in ("systemctl start", "systemctl stop", "systemctl enable", "systemctl disable"):
        assert forbidden not in maintainer_scripts
    assert "/opt/cyrene/python/3.12.14/bin/python3.12" in postinst

    control = source.split('cat <<EOF > "${STAGE_DIR}/DEBIAN/control"\n', 1)[1].split("\nEOF\n", 1)[
        0
    ]
    depends = next(line for line in control.splitlines() if line.startswith("Depends:"))
    assert "python3" not in depends
    assert "python3-jsonschema" not in depends
    unit_stanzas = source.split("# 4. Systemd service units", 1)[1].split("# 5. DEBIAN/control", 1)[
        0
    ]
    assert (
        unit_stanzas.count(
            "ExecStart=/opt/cyrene/python/3.12.14/bin/python3.12 -sE /usr/lib/cyrene/scripts/cyrene.py service-run "
        )
        == 5
    )
    contract_source = (WORKSPACE_ROOT / "packaging" / "native_install_contract.py").read_text(
        encoding="utf-8"
    )
    assert '"serviceActivation": "deferred"' in contract_source


def _kernel_service_account_block() -> str:
    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    postinst = source.split("cat <<'EOF' > \"${STAGE_DIR}/DEBIAN/postinst\"\n", 1)[1].split(
        '\nEOF\nchmod 755 "${STAGE_DIR}/DEBIAN/postinst"', 1
    )[0]
    start = postinst.index("# The signed Kernel unit runs as a dedicated system account.")
    end = postinst.index('CYRENE_UID="$(id -u cyrene)"', start)
    return postinst[start:end]


def _run_kernel_service_account_block(
    tmp_path: Path, account_state: Path
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    id_stub = fake_bin / "id"
    id_stub.write_text(
        "#!/bin/sh\n"
        '[ "$#" -eq 2 ] && [ "$1" = "-u" ] && [ "$2" = "cyrene-kernel" ] || exit 64\n'
        '[ -f "$CYRENE_TEST_ACCOUNT_STATE" ] || exit 1\n'
        'IFS=: read -r uid gid home shell < "$CYRENE_TEST_ACCOUNT_STATE" || exit 1\n'
        'printf "%s\\n" "$uid"\n',
        encoding="utf-8",
    )
    id_stub.chmod(0o755)
    useradd_stub = fake_bin / "useradd"
    useradd_stub.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "--system --user-group --no-create-home --shell /usr/sbin/nologin cyrene-kernel" ] || exit 64\n'
        '[ ! -e "$CYRENE_TEST_ACCOUNT_STATE" ] || exit 17\n'
        'printf "997:997:/nonexistent:/usr/sbin/nologin\\n" > "$CYRENE_TEST_ACCOUNT_STATE"\n'
        'printf "created\\n" >> "$CYRENE_TEST_USERADD_LOG"\n',
        encoding="utf-8",
    )
    useradd_stub.chmod(0o755)
    return subprocess.run(
        ["/bin/sh", "-eu", "-c", _kernel_service_account_block()],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": str(fake_bin),
            "CYRENE_TEST_ACCOUNT_STATE": str(account_state),
            "CYRENE_TEST_USERADD_LOG": str(tmp_path / "useradd.log"),
        },
    )


def _postinst_owned_directory_function() -> str:
    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    postinst = source.split("cat <<'EOF' > \"${STAGE_DIR}/DEBIAN/postinst\"\n", 1)[1].split(
        '\nEOF\nchmod 755 "${STAGE_DIR}/DEBIAN/postinst"', 1
    )[0]
    start = postinst.index("ensure_owned_directory() {")
    end = postinst.index("\n}\n\nensure_fresh_service_directory()", start) + 2
    return postinst[start:end]


def _postinst_navigator_pair_code_migration_function() -> str:
    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    postinst = source.split("cat <<'EOF' > \"${STAGE_DIR}/DEBIAN/postinst\"\n", 1)[1].split(
        '\nEOF\nchmod 755 "${STAGE_DIR}/DEBIAN/postinst"', 1
    )[0]
    start = postinst.index("migrate_navigator_pair_code() {")
    end = postinst.index("\nmigrate_navigator_pair_code /var/lib/cyrene/", start)
    return postinst[start:end]


def _systemd_unit(source: str, service: str) -> str:
    marker = f"cat <<'EOF' > \"${{STAGE_DIR}}/lib/systemd/system/cyrene-{service}.service\"\n"
    start = source.index(marker) + len(marker)
    end = source.index("\nEOF\n", start)
    return source[start:end]


def _staged_environment_file(source: str, name: str) -> str:
    marker = f"cat <<'EOF' > \"${{STAGE_DIR}}/etc/cyrene/{name}.env\"\n"
    start = source.index(marker) + len(marker)
    end = source.index("\nEOF\n", start)
    return source[start:end]


def test_postinst_owned_directory_validation_never_repairs_existing_state(
    tmp_path: Path,
) -> None:
    function = _postinst_owned_directory_function()
    uid = os.getuid()
    gid = os.getgid()
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o750)
    safe.chmod(0o750)
    accepted = subprocess.run(
        [
            "/bin/sh",
            "-eu",
            "-c",
            function + '\nensure_owned_directory "$1" "$2" "$3" 750 test',
            "test",
            str(safe),
            str(uid),
            str(gid),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr

    wrong_owner = subprocess.run(
        [
            "/bin/sh",
            "-eu",
            "-c",
            function + '\nensure_owned_directory "$1" 0 "$2" 750 test',
            "test",
            str(safe),
            str(gid),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert wrong_owner.returncode != 0
    assert "Refusing to repair existing test" in wrong_owner.stderr
    metadata = safe.lstat()
    assert metadata.st_uid == uid
    assert stat.S_IMODE(metadata.st_mode) == 0o750

    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    assert 'ensure_owned_directory /var/lib/cyrene 0 "${CYRENE_GID}" 750' in source
    assert '"/var/lib/cyrene/${product}"' in source
    assert "Legacy Exchange database state exists" in source
    assert '"${CYRENE_UID}:${CYRENE_GID}:750"' in source
    assert "STUDIO_CONTROL_UID" not in source
    assert "STUDIO_CONTROL_GID" not in source


def test_product_units_load_private_path_defaults_after_shared_environment() -> None:
    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    defaults = {
        "navigator": ("CYRENE_PAIR_CODE_FILE", "/var/lib/cyrene/navigator/pair_code.txt"),
        "yield": ("CYRENE_ARTIFACT_ROOT", "/var/lib/cyrene/yield/artifacts"),
        "exchange": ("CYRENE_DATA_DIR", "/var/lib/cyrene/exchange"),
    }
    shared = _staged_environment_file(source, "cyrene")

    for service, (variable, expected) in defaults.items():
        unit = _systemd_unit(source, service)
        unit_lines = unit.splitlines()
        service_environment = f"/etc/cyrene/{service}.env"
        environment_files = [
            line.removeprefix("EnvironmentFile=").removeprefix("-")
            for line in unit_lines
            if line.startswith("EnvironmentFile=")
        ]
        assert environment_files[-1] == service_environment
        assert not any(line.startswith(f"Environment={variable}=") for line in unit_lines)

        # systemd applies Environment= first, then EnvironmentFile values in
        # file order; later files therefore supply the effective private path.
        effective: dict[str, str] = {}
        for line in unit_lines:
            if line.startswith("Environment="):
                key, value = line.removeprefix("Environment=").split("=", 1)
                effective[key] = value
        files = {
            "/etc/cyrene/cyrene.env": shared,
            service_environment: _staged_environment_file(source, service),
        }
        for path in environment_files:
            for entry in files.get(path, "").splitlines():
                if entry and not entry.startswith("#"):
                    key, value = entry.split("=", 1)
                    effective[key] = value
        assert effective[variable] == expected

    for service in SERVICES:
        unit = _systemd_unit(source, service.removeprefix("cyrene-"))
        assert not any(line == "StateDirectory=cyrene" for line in unit.splitlines())


def test_navigator_pair_code_migration_copies_atomically_and_preserves_source(
    tmp_path: Path,
) -> None:
    function = _postinst_navigator_pair_code_migration_function()
    shared_root = tmp_path / "cyrene"
    navigator_dir = shared_root / "navigator"
    navigator_dir.mkdir(parents=True, mode=0o750)
    legacy = shared_root / "pair_code.txt"
    legacy.write_text("PAIR-CODE-FIXTURE\n", encoding="utf-8")
    legacy.chmod(0o600)
    destination = navigator_dir / "pair_code.txt"
    uid, gid = os.getuid(), os.getgid()
    result = subprocess.run(
        [
            "/bin/sh",
            "-eu",
            "-c",
            function + '\nmigrate_navigator_pair_code "$1" "$2"',
            "test",
            str(legacy),
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CYRENE_UID": str(uid), "CYRENE_GID": str(gid)},
    )

    assert result.returncode == 0, result.stderr
    assert legacy.read_text(encoding="utf-8") == "PAIR-CODE-FIXTURE\n"
    assert destination.read_bytes() == legacy.read_bytes()
    assert (destination.stat().st_uid, destination.stat().st_gid) == (uid, gid)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(shared_root.glob(".navigator-pair-code.*"))
    assert 'mktemp "${source%/*}/.navigator-pair-code.XXXXXXXX"' in function


def test_navigator_pair_code_migration_refuses_to_replace_conflicting_state(
    tmp_path: Path,
) -> None:
    function = _postinst_navigator_pair_code_migration_function()
    shared_root = tmp_path / "cyrene"
    navigator_dir = shared_root / "navigator"
    navigator_dir.mkdir(parents=True, mode=0o750)
    legacy = shared_root / "pair_code.txt"
    legacy.write_text("legacy code\n", encoding="utf-8")
    destination = navigator_dir / "pair_code.txt"
    destination.write_text("current code\n", encoding="utf-8")
    destination.chmod(0o600)
    uid, gid = os.getuid(), os.getgid()
    result = subprocess.run(
        [
            "/bin/sh",
            "-eu",
            "-c",
            function + '\nmigrate_navigator_pair_code "$1" "$2"',
            "test",
            str(legacy),
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CYRENE_UID": str(uid), "CYRENE_GID": str(gid)},
    )

    assert result.returncode != 0
    assert "differ; refusing to choose one" in result.stderr
    assert legacy.read_text(encoding="utf-8") == "legacy code\n"
    assert destination.read_text(encoding="utf-8") == "current code\n"


def test_postinst_refuses_existing_exchange_database_without_modifying_it(
    tmp_path: Path,
) -> None:
    source = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    start = source.index("for legacy_exchange_file in /var/lib/cyrene/exchange.sqlite3")
    end = source.index("\ndone", start) + len("\ndone")
    check = source[start:end].replace("/var/lib/cyrene", str(tmp_path))
    paths = {
        tmp_path / "exchange.sqlite3": b"database",
        tmp_path / "exchange.sqlite3-wal": b"wal",
        tmp_path / "exchange.sqlite3-shm": b"shm",
    }
    for path, content in paths.items():
        path.write_bytes(content)

    result = subprocess.run(
        ["/bin/sh", "-eu", "-c", check], check=False, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "explicit data migration" in result.stderr
    assert {path: path.read_bytes() for path in paths} == paths


def test_postinst_provisions_kernel_service_user_idempotently(tmp_path: Path) -> None:
    account_state = tmp_path / "account"
    first = _run_kernel_service_account_block(tmp_path, account_state)
    assert first.returncode == 0, first.stderr
    assert account_state.read_text(encoding="utf-8") == "997:997:/nonexistent:/usr/sbin/nologin\n"
    calls = tmp_path / "useradd.log"
    assert calls.read_text(encoding="utf-8") == "created\n"

    second = _run_kernel_service_account_block(tmp_path, account_state)
    assert second.returncode == 0, second.stderr
    assert account_state.read_text(encoding="utf-8") == "997:997:/nonexistent:/usr/sbin/nologin\n"
    assert calls.read_text(encoding="utf-8") == "created\n"


def test_postinst_preserves_existing_kernel_user_without_primary_group_contract(
    tmp_path: Path,
) -> None:
    account_state = tmp_path / "account"
    existing_account = "1500:2200:/home/existing:/bin/bash\n"
    account_state.write_text(existing_account, encoding="utf-8")

    result = _run_kernel_service_account_block(tmp_path, account_state)

    assert result.returncode == 0, result.stderr
    assert account_state.read_text(encoding="utf-8") == existing_account
    assert not (tmp_path / "useradd.log").exists()


def test_postinst_rejects_root_kernel_service_account_without_repair(tmp_path: Path) -> None:
    account_state = tmp_path / "account"
    root_account = "0:0:/root:/bin/bash\n"
    account_state.write_text(root_account, encoding="utf-8")

    result = _run_kernel_service_account_block(tmp_path, account_state)

    assert result.returncode != 0
    assert "cyrene-kernel account must not use UID 0" in result.stderr
    assert account_state.read_text(encoding="utf-8") == root_account
    assert not (tmp_path / "useradd.log").exists()
