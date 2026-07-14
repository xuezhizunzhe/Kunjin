# KunJin Docker Desktop Credential Helper Compatibility Design

## Goal

Allow the reviewed legacy-document converter build script to use Docker
Desktop's configured `docker-credential-desktop` helper without weakening the
script's fixed-path and fail-closed supply-chain controls.

## Root Cause

The script resets `PATH` to `/usr/bin:/bin`. The local Docker configuration uses
`credsStore=desktop`, so Docker resolves `docker-credential-desktop` before it
can fetch the public Dockerfile frontend or pinned Debian base image. Docker
Desktop installs that helper under its application bundle, outside the current
fixed `PATH`.

## Design

The script will define one fixed trusted Docker Desktop binary directory:

```text
/Applications/Docker.app/Contents/Resources/bin
```

It will prepend only that directory to the existing `/usr/bin:/bin` path. The
script will reject setup unless both the expected Docker CLI and
`docker-credential-desktop` are regular, non-symlink, executable files at their
exact application-bundle paths. Caller-provided path entries remain ignored.

The build continues to invoke Docker through the already authenticated
`/usr/local/bin/docker` link. The helper is resolved only when Docker requests
the configured `desktop` credential store.

## Rejected Alternatives

- A temporary empty `DOCKER_CONFIG` would change authentication, context, and
  anonymous rate-limit behavior.
- Editing `~/.docker/config.json` or installing a new system link would mutate
  unrelated host configuration.
- Adding broad user or package-manager directories to `PATH` would expand the
  executable search surface beyond the existing Docker Desktop trust boundary.

## Security And Failure Behavior

- The pinned Debian digest, exact LibreOffice package version, reviewed
  Dockerfile checksum, two-stage manifest verification, and cleanup behavior do
  not change.
- Runtime conversion remains network-disabled and non-root.
- A missing, linked, non-regular, or non-executable credential helper closes the
  build with the existing setup-prerequisite failure class.
- No credential content, image identifier, container identifier, or raw Docker
  output is added to KunJin's JSON interface.

## Verification

- Add static assertions for the exact trusted path and helper validation.
- Copy the script and authenticated Dockerfile into an isolated temporary tree,
  substitute only test-local Docker paths, and verify that a missing,
  non-regular, non-executable, or symlinked credential helper exits with code
  `69` before any Docker command can run.
- Retain the malicious caller-`PATH` regression test.
- Run the focused smoke tests, Bash syntax validation, and the full repository
  test suite.
- Repeat the real pinned build, then require safe
  `fund converter-status.status=ready` before live document acceptance.
