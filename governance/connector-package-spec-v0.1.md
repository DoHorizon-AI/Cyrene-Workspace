# Cyrene Connector Package Spec v0.1

## Purpose

This is the smallest package contract for a replaceable connector that is
resolved through the existing Platform PluginManifest, PluginSetLock, and
node-local PluginStore seams. It describes how a connector can be made
available and verified; it does not define a marketplace, a Product
configuration UI, or connector-specific business policy.

The machine-readable authority is
`governance/connector-package-spec-v0.1.schema.json`. The connector catalog
in `governance/connectors.yaml` is a static candidate projection and points to
the same schema.

## Identity and authority

`package.id` and `package.version` are the canonical PluginManifest `id` and
`version`. They are repeated in this descriptor only so a catalog consumer can
cross-check the manifest reference; they must never be independently edited.

`capability.id` describes what the implementation exposes, for example
`message.connector.v1`. `connector.family` and `connector.type` classify the
implementation. A configured capability instance is a separate Platform
binding and is not represented by this package identity.

The package descriptor is static metadata. Platform resolution pins its
artifact through the existing `PluginSetLock`; the descriptor does not create
a second registry or resolver.

## Required package data

- `implementation.artifact` is either `PUBLISHED` with a URI and a
  `sha256:<64-hex>` digest, or `NOT_PUBLISHED` for a candidate that has no
  honest artifact yet.
- `dependencies.lock` is either `LOCKED` with an exact lock-file reference,
  format, and digest, or `NOT_PUBLISHED`. Production packages may not mean
  `pip install latest` or an equivalent floating install.
- `configuration.schema_ref` identifies the configuration schema supplied by
  the implementation. Secret values are never stored in a manifest or this
  descriptor.
- `runtime.kind` uses the Platform vocabulary (`subprocess-python`,
  `subprocess-jvm`, or `service`). `runtime.external` declares a required
  external runtime without naming vendor/Product semantics.
- `integrity` carries artifact/archive digests and an optional signature
  reference. A `PUBLISHED` descriptor requires both digests.
- `lifecycle.rollback_identity` is the immutable package version identity;
  `lifecycle.cache_identity` is the content-addressed cache key. Neither is a
  worker PID or runtime generation.
- `compatibility` records the Platform and capability interface constraints.
- `provenance` leaves hooks for source revision, builder, SBOM, and attestation
  references. Missing SBOM/attestation data is represented as `null`, not as a
  false claim.

## Static catalog versus dynamic installation

The descriptor's `publication_status` is limited to `NOT_PUBLISHED`,
`CANDIDATE`, and `PUBLISHED`. It must not contain `INSTALLED`, `RUNNING`, or
`CONFIGURED` state. Those states belong to a node-local installation record,
which is the second record type in the schema and is owned by the existing
Python/C# PluginStore implementations.

The v0.1 installation record is intentionally small: package identity,
version, verified artifact digest, content-addressed cache identity, current
state, active version, rollback identity, and bounded version history. The
existing store already provides offline archive verification, safe extraction,
activation, rollback, and cache storage. A future store persistence change may
materialize this record without changing the static catalog contract.

Supported installation states are `STAGED`, `INSTALLED`, `ACTIVE`, `FAILED`,
`ROLLED_BACK`, and `REMOVED`. Runtime liveness and worker generation remain
observations of the active runtime; they are not package identity.

## Candidate first connector

The OneBot v11 catalog entry is a `CANDIDATE` package descriptor. It records
the generic `message.connector.v1` capability, the `aiocqhttp` implementation
type, external protocol-runtime separation, and the configuration source, but
does not invent a package artifact, dependency lock digest, signature, SBOM,
or installation state. Task 4 may promote a descriptor only after an actual
immutable artifact and exact dependency lock are produced.
