"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 export_authority_keyring.py                                          │
│  Module: tooling.deploy.teams_cloud.export_authority_keyring            │
│  Role: Derive a public-only Ed25519 keyring from Authority seed files.   │
│                                                                          │
│  模块职责：从 Authority 私钥种子派生仅含公钥的 keyring。                 │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KEY_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
SPKI_ED25519_PREFIX = bytes.fromhex("302a300506032b6570032100")
MIN_ROTATION_OVERLAP_SECONDS = 45


class KeyringError(ValueError):
    """A private key file or public-key derivation did not meet the contract."""


@dataclass(frozen=True)
class KeySource:
    key_id: str
    private_key_file: Path
    not_before: datetime
    not_after: datetime


def _timestamp(value: str) -> datetime:
    """Parse one timezone-aware RFC3339 timestamp."""

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise KeyringError("key validity timestamps must be RFC3339") from error
    if result.tzinfo is None:
        raise KeyringError("key validity timestamps must include a timezone")
    return result.astimezone(UTC)


def parse_key_source(value: str) -> KeySource:
    """Parse key-id|seed-file|not-before|not-after without reading the seed."""

    parts = value.split("|", 3)
    if len(parts) != 4:
        raise KeyringError("each --key must use key-id|private-file|not-before|not-after")
    key_id, filename, not_before, not_after = parts
    if not KEY_ID_RE.fullmatch(key_id):
        raise KeyringError("key ID is invalid")
    start, end = _timestamp(not_before), _timestamp(not_after)
    if start >= end:
        raise KeyringError("key validity window is empty")
    return KeySource(key_id, Path(filename), start, end)


def _public_key(seed_file: Path) -> bytes:
    """Derive the raw 32-byte Ed25519 public key; never write the seed."""

    if seed_file.is_symlink() or not seed_file.is_file():
        raise KeyringError("Authority seed must be a regular non-symlink file")
    metadata = seed_file.stat()
    if metadata.st_mode & 0o077:
        raise KeyringError("Authority seed file must not be group- or world-readable")
    seed = bytearray(seed_file.read_bytes())
    if len(seed) != 32:
        seed[:] = b"\0" * len(seed)
        raise KeyringError("Authority signing seed must contain exactly 32 raw bytes")
    private_der = bytearray(PKCS8_ED25519_PREFIX + seed)
    seed[:] = b"\0" * len(seed)
    try:
        result = subprocess.run(
            ["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
            input=bytes(private_der),
            capture_output=True,
            check=False,
        )
    finally:
        private_der[:] = b"\0" * len(private_der)
    if result.returncode != 0 or not result.stdout.startswith(SPKI_ED25519_PREFIX):
        raise KeyringError("OpenSSL could not derive an Ed25519 public key")
    public_key = result.stdout[len(SPKI_ED25519_PREFIX) :]
    if len(public_key) != 32:
        raise KeyringError("derived Ed25519 public key has an invalid length")
    return public_key


def export_keyring(
    keys: list[KeySource], output: Path, *, now: datetime | None = None
) -> dict[str, object]:
    """Create an Exchange-compatible JSON map containing public keys only."""

    if not keys:
        raise KeyringError("at least one Authority signing key is required")
    if len({key.key_id for key in keys}) != len(keys):
        raise KeyringError("key IDs must be unique")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    active = [key for key in keys if key.not_before <= current < key.not_after]
    if not active:
        raise KeyringError("no Authority key is active at the current time")
    if len(keys) > 1:
        ordered = sorted(keys, key=lambda row: row.not_before)
        for previous, following in itertools.pairwise(ordered):
            overlap = (
                min(previous.not_after, following.not_after)
                - max(previous.not_before, following.not_before)
            ).total_seconds()
            if overlap <= MIN_ROTATION_OVERLAP_SECONDS:
                raise KeyringError("Authority key rotation overlap must exceed 45 seconds")
    key_map: dict[str, str] = {}
    for row in active:
        key_map[row.key_id] = base64.b64encode(_public_key(row.private_key_file)).decode("ascii")
    document: dict[str, object] = {"keys": key_map}
    if output.exists() or output.is_symlink():
        raise KeyringError("public keyring output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o644)
    temporary.replace(output)
    return document


def main() -> int:
    """Derive and write the public-only Exchange Authority keyring."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key", action="append", default=[], metavar="ID|SEED_FILE|NOT_BEFORE|NOT_AFTER"
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        sources = [parse_key_source(value) for value in arguments.key]
        document = export_keyring(sources, arguments.output.resolve())
    except (OSError, KeyringError, subprocess.SubprocessError) as error:
        print(f"Authority keyring export blocked: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "PUBLIC_KEYRING_EXPORTED", "keyIds": sorted(document["keys"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
