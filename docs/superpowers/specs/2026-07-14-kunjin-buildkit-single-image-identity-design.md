# KunJin BuildKit Single-Image Identity Design

## Goal

Keep the reviewed legacy-document converter build compatible with Docker
Desktop 29 while preserving KunJin's exact local image-ID authentication.

## Observed Failure

The real probe build successfully pulled the pinned Debian image, installed the
exact no-GUI LibreOffice package, and exported its layers. Docker Desktop 29
also exported a provenance attestation and an OCI manifest list by default. The
script then stopped in its silent post-build authentication boundary before the
final build began.

The script contract expects `--iidfile` to identify one locally inspectable
`linux/arm64` image whose Docker `.Id` is the same value. A default attestation
index introduces an additional outer identity and is incompatible with that
single-image contract.

## Selected Design

Both probe and final builds will pass:

```text
--provenance=false
```

This suppresses Docker's automatically attached outer provenance index. It does
not remove KunJin's own authenticated package manifest, fixed labels, base-image
digest, exact LibreOffice version, Dockerfile checksum, or parser provenance.

The script will also emit bounded setup-stage labels to stderr before each major
boundary:

```text
probe_build
probe_identity
probe_manifest
final_build
final_identity
final_manifest
ready
```

Only these fixed labels are allowed. They contain no image ID, tag, container
ID, path, package content, Docker response, or exception text. The existing
wrapper keeps them in the private build log.

## Rejected Alternatives

- Parsing Docker's OCI index and metadata file would add a second identity model
  and a new JSON-parser dependency to a security-sensitive setup script.
- Treating the private build tag as the authoritative result would weaken the
  exact image-ID contract.
- Relaxing the `iidfile == docker image inspect .Id` check would accept an
  ambiguous build result instead of restoring the expected output shape.

## Security And Failure Behavior

- The fixed Debian digest, exact package version, reviewed Dockerfile checksum,
  package-manifest checksum, platform check, labels, and cleanup remain required.
- Probe and final builds remain `--pull`, `--no-cache`, and `linux/arm64`.
- Runtime conversion remains `--pull=never`, `--network=none`, and non-root.
- Any identity or manifest mismatch continues to fail closed.
- Stage labels improve local diagnosis but never enter KunJin's JSON interface.

## Verification

- Assert exactly two `--provenance=false` build arguments.
- Assert the complete allowlisted stage set and reject dynamic stage text.
- Keep all existing IID, tag-absence, cleanup, package, and Dockerfile checks.
- Run focused smoke tests, Bash syntax, full tests, Ruff, compileall, dependency
  checks, and an independent read-only review before the next real build.
