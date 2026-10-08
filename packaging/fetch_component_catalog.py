#!/usr/bin/env python3
"""Command-line entrypoint for fetching one verified component catalog release.

The CLI delegates GitHub attestation subject checks to the existing component
updater verifier and prints metadata only after both files have been persisted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from catalog_metadata import (
    CHANNELS,
    TAG_PATTERN,
    CatalogMetadataError,
    fetch_verified_catalog,
)


def _attestation_verifier() -> Any:
    """Return the existing component updater's exact SLSA subject verifier."""

    try:
        from component_updates import ComponentUpdater

        updater = ComponentUpdater(
            catalog_path=(
                Path(__file__).resolve().parents[1] / "governance" / "component-catalog-v1.json"
            ),
            trusted_catalog_digest=None,
            load_active_catalog=False,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        raise CatalogMetadataError(
            f"Cannot initialize the component attestation verifier: {error}"
        ) from error
    return updater._verify_attestation


def _default_schema_root() -> Path:
    """Prefer installed catalog schemas and fall back to the repository copy."""

    try:
        from component_updates import CATALOG_SCHEMA_ROOT
    except ImportError as error:
        raise CatalogMetadataError(
            f"Cannot locate the component catalog schemas: {error}"
        ) from error
    if CATALOG_SCHEMA_ROOT.is_dir():
        return CATALOG_SCHEMA_ROOT
    return Path(__file__).resolve().parents[1] / "governance"


def _parser() -> argparse.ArgumentParser:
    """Build the strict release-selection and output argument parser."""

    parser = argparse.ArgumentParser(prog="fetch_component_catalog")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--release-id",
        help="Fetch this exact immutable v1 catalog-{stable|preview}-<40hex> or v2 catalog-v2-{stable|preview}-<40hex> release.",
    )
    selection.add_argument(
        "--latest-channel",
        choices=sorted(CHANNELS),
        help="Fetch the newest immutable release from this channel.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Catalog asset output path.")
    parser.add_argument(
        "--metadata-output", type=Path, required=True, help="Verified metadata output path."
    )
    parser.add_argument(
        "--attestation-output",
        type=Path,
        help="Detached attestation bundle output path (defaults to OUTPUT.attestation.jsonl).",
    )
    parser.add_argument(
        "--schema-root",
        type=Path,
        help="Directory with catalog v1 and manifest v1/v2 schemas (defaults to installed schemas).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Fetch the selected release and emit its validated metadata as JSON."""

    args = _parser().parse_args(argv)
    if args.release_id is not None:
        match = TAG_PATTERN.fullmatch(args.release_id)
        if match is None:
            print(
                "release-id must be catalog-{stable|preview}-<40 lowercase hex> or catalog-v2-{stable|preview}-<40 lowercase hex>.",
                file=sys.stderr,
            )
            return 2
        channel = match.group(1)
        release_id = args.release_id
        latest_channel = None
    else:
        channel = args.latest_channel
        release_id = None
        latest_channel = args.latest_channel

    try:
        metadata = fetch_verified_catalog(
            channel=channel,
            release_id=release_id,
            latest_channel=latest_channel,
            output_path=args.output,
            metadata_path=args.metadata_output,
            schema_root=args.schema_root or _default_schema_root(),
            verify_attestation=_attestation_verifier(),
            attestation_output_path=args.attestation_output,
        )
    except CatalogMetadataError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
