# Cyrene Product HTTP and evolution profile v1

This document is the single cross-Product authority for HTTP evolution, errors,
idempotency, asynchronous actions, and trace propagation. Product OpenAPI files
declare `x-cyrene-contract-profile: product-http-v1` and define only their own
resources, commands, states, and Product-specific error codes.

## Identity

- Product resource IDs are stable and opaque. UUID wire format is preferred;
  an explicitly opaque string is allowed where a pre-existing Workspace ID is
  preserved.
- API identity never exposes a database row key, filesystem path, PID, worker,
  lease, fence token, executable, provider identity, or plugin package identity.
- A Product resource ID is never a Kernel Operation ID. `DeploymentId` and
  `EndpointId`, and capability `BindingId` and provider identity, remain
  different concepts.
- Cross-Product fields are references to owner resources; they do not transfer
  resource authority to the consumer.

## Compatibility and deprecation

- Major: any breaking semantic or wire change, including changed state meaning,
  removed fields or enum values, or a newly required input.
- Minor: backward-compatible optional fields, operations, and enum values whose
  unknown handling is documented. Old consumers must ignore unknown optional
  object fields.
- Patch: semantic-preserving correction or clarification only.
- OpenAPI operations, parameters, or schemas begin deprecation with
  `deprecated: true` and a migration link. Removal is allowed only in the next
  major version after at least 180 days and one published minor-version
  migration window. A security issue may shorten the window only with a
  published exception and replacement path.

## Problem Details

Product control APIs use RFC 9457 `application/problem+json`. The canonical
schema is `problem-details.schema.json` and requires `type`, `title`, `status`,
`detail`, `instance`, stable uppercase `code`, `retryable`, and W3C-derived
`traceId`; `resourceRef` is optional. Generated Product copies must be byte-for-
byte semantically equal to this schema and are not authorities.

Problem payloads must not expose filesystem paths, PIDs, SQL text, raw
exceptions, secrets, tokens, worker executables, provider package internals, or
stack traces. Product resource failure records use stable codes and sanitized
messages; rejected HTTP commands use Problem Details.

## Asynchronous actions

RFC 7240 defines the preference, while this profile completes the lifecycle:

1. A supported action receiving `Prefer: respond-async` returns `202 Accepted`,
   `Preference-Applied: respond-async`, and `Location` naming the stable
   Product-owned resource used for polling.
2. `GET Location` returns current Product state, resource version, terminal
   state, and a sanitized Product failure when applicable. The location never
   names or aliases a Kernel Operation.
3. Cancellation is a Product command on that resource. Acceptance returns
   `202` and the same Product `Location`; cancellation intent is not terminal
   proof. A terminal state requires reconciled execution evidence.
4. Transport retry reuses the same `Idempotency-Key`. Product policy retry uses
   the documented Product Run/Attempt/Generation model and never reuses a
   Kernel lease or fence as HTTP idempotency.
5. `traceparent` and optional `tracestate` correlate the request, response, and
   downstream calls. Product ID, idempotency key, and trace ID are distinct.

Synchronous reference MVP actions return `201` only after their Product
resource reaches the represented state. They do not advertise async behavior
that they have not implemented.

## Idempotency

`Idempotency-Key` is a Cyrene-defined request header because the IETF draft is
not a stable RFC. Scope is authenticated principal plus operation plus owning
resource where applicable. A replay with the same key and canonical request
body returns the current representation of the same Product resource and the
operation's normal success status (`201` or `202`). Reuse with a different body
returns `409` and a stable Product-specific conflict code.

The mapping is retained at least for the lifetime of the referenced durable
resource; Products with deletion must publish a longer bounded retention policy
before expiring keys. HTTP idempotency never substitutes for Kernel lease,
generation, or fence correctness.

## Trace Context

The normative dependency is W3C Trace Context Level 1, Recommendation of
23 November 2021: `traceparent` and `tracestate`. All-zero trace or parent IDs
and syntactically invalid version `00` values are rejected as incoming identity;
the service starts a new valid context rather than propagating them. Trace
Context Level 2 is only forward-compatible experimental support until it becomes
a W3C Recommendation and is not a Product v1 normative dependency.
