"""Focused tests for the stage-only DEB contract evidence marker."""

from __future__ import annotations

import hashlib
import importlib.util
import json
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


def test_contract_binds_the_exact_index_bytes_and_maintainer_scripts(tmp_path: Path) -> None:
    module = _module()
    index_path = tmp_path / "service-artifacts" / "index.json"
    index_path.parent.mkdir()
    index_bytes = (json.dumps(_index(), indent=2) + "\n").encode()
    index_path.write_bytes(index_bytes)
    scripts = tmp_path / "DEBIAN"
    _scripts(scripts)
    output = tmp_path / "contract.json"

    result = module.create_contract(
        target_profile=TARGET,
        service_artifacts_index=index_path,
        scripts_dir=scripts,
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

    with pytest.raises(module.ContractError):
        module.create_contract(
            target_profile=TARGET,
            service_artifacts_index=index_path,
            scripts_dir=scripts,
            output=tmp_path / "contract.json",
        )


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
