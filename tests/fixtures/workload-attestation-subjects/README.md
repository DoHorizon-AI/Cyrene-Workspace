# Workload attestation subject fixtures

These are the exact published v1 index and manifest bytes used to check the
legacy trust-chain adapter. The fixtures do not contain signature bundles;
runtime verification still obtains and verifies those through GitHub.

| Component | Repository source | Immutable release tag | Index SHA-256 | Manifest SHA-256 |
| --- | --- | --- | --- | --- |
| Catalyst Ubuntu 24.04 | `DoHorizon-AI/Cyrene-Catalyst@4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` | `preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` | `fc012af0193e3fdb5abc3967383977fcf0ff7dc3dd12238e07fa8aa9dd4ffdfa` | `cbea66b69b7087872e911dc034229000ae11cfd7b3c31942b1d13eb48c49b1a8` |
| Runtime Maintenance SDK Ubuntu 24.04 | `DoHorizon-AI/Cyrene-Platform@1ec629e55195fa803651fb23d1c50d04696a94a8` | `preview-1ec629e55195fa803651fb23d1c50d04696a94a8` | `89bb735f695b66bc08a7f8ea5ad971bc237dc9822208a64e47986ec79d4547c1` | `0ffc6db33002fae16788c7d5a1600b43baf4e674e1c7fc3618a8f80aba028c87` |
| Echo Ubuntu 24.04 OCI | `DoHorizon-AI/Cyrene-Echo@d5a920078077bdbaa76e7117ad83f69cbfa1c614` | `preview-d5a920078077bdbaa76e7117ad83f69cbfa1c614` | `48196a2acbe8e6b5704e332a8562b8ff11a821781dd2e879097d70cee41511de` | `1106d990493a617cc83d21fae409929ccb0ffc71573701e523bf4038059e99a7` |

The fixture tests recompute each manifest's JCS digest and preserve the
distinction between that digest, the manifest's downloaded-byte digest, and
the actual archive or OCI image subject digest.
