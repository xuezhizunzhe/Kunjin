# KunJin BuildKit Manifest And Config Identity Design

## Goal

Authenticate Docker Desktop 29 build output without assuming that Buildx's
iidfile evidence has one fixed digest meaning or can be trusted directly as the
runtime image identity.

## Confirmed Behavior

With Docker-generated provenance disabled, the real probe build exported one
manifest digest and one config digest, then stopped at `probe_identity`.
Buildx v0.34.1 writes the result of `getImageID` to `--iidfile` without a final
newline. That function prefers the exporter config digest when present and
falls back to the exporter image digest. The iidfile is therefore private build
evidence, while `docker image inspect .Id` remains the authoritative identity of
the locally loaded image used by the converter.

The setup must not depend on iidfile equality, inequality, or a fixed
manifest-versus-config interpretation. It authenticates the local image through
the private tag and exact config image ID instead.

## Selected Identity Chain

1. Generate a random private build tag and prove it does not exist.
2. Build with the pinned inputs, `--provenance=false`, and `--iidfile`.
3. Require the iidfile to contain one valid lowercase SHA-256 build-result
   digest, encoded as 71 bytes or with exactly one trailing LF as 72 bytes.
4. Inspect the newly created private tag and require one exact config image ID,
   `linux/arm64`, and no extra fields.
5. Inspect that exact config ID and require the same ID and platform. For the
   final image, also require every KunJin label and package-manifest checksum.
6. Re-inspect the private tag by ID after exact-ID authentication and require it
   still resolves to the same image, closing the setup-time retagging window.
7. Use only the exact config image ID for manifest extraction, final output, and
   converter runtime configuration.

The iidfile digest remains private setup evidence. It is never presented as the
runtime image ID and is not exposed through KunJin JSON.

## Cleanup

Cleanup no longer treats iidfile content as a removable local image ID. If an
exact config ID has been resolved, cleanup may remove by that ID. In every
failure path, the owned random tag is also removed. If failure occurs before ID
resolution, tag removal is the authoritative cleanup path.

## Rejected Alternatives

- `--metadata-file` would require a new JSON metadata parser and Buildx schema
  dependency in the trusted setup script.
- Using the iidfile digest directly at runtime would couple runtime identity to
  Buildx exporter behavior instead of the authenticated local image.
- Trusting the tag without resolving and rechecking an immutable ID would weaken
  the runtime boundary.

## Security And Compatibility

- The random tag is setup-only, checked absent before build, and rechecked after
  exact-ID authentication. Runtime conversion never invokes a tag.
- Fixed Debian digest, exact LibreOffice version, reviewed Dockerfile checksum,
  package manifest, platform, labels, no-network runtime, and fail-closed cleanup
  remain mandatory.
- Safe setup-stage labels remain unchanged.

## Verification

- Add tests that distinguish iidfile build digest variables from config image
  ID variables.
- Accept Buildx's no-final-newline iidfile output while rejecting CR, NUL,
  embedded newlines, multiple trailing newlines, and malformed digests.
- Lock tag-before-build absence, tag-to-ID resolution, exact-ID inspection, and
  tag recheck ordering for both probe and final builds.
- Remove iidfile-based image cleanup assumptions.
- Run full verification and independent review before another real build.
