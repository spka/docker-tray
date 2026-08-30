# Releasing Docker Tray

Releases are built from annotated version tags by GitHub Actions. Do not build
or upload release packages from a workstation.

1. Update the version in `docker_tray.py` and `package-deb.sh`, along with
   version-specific tests and README links.
2. Run `./scripts/release-version` and the test suite.
3. Commit and push the release changes to `main`.
4. Create and push an annotated tag:

   ```bash
   git tag -a vX.Y.Z -m "Docker Tray X.Y.Z"
   git push origin vX.Y.Z
   ```

The `Publish release` workflow rejects malformed or mismatched versions and
tags whose commit is not contained in `origin/main`. It then runs the tests,
builds and inspects the Debian package, creates a SHA-256 sidecar, records a
GitHub provenance attestation, uploads both assets to a draft, and publishes
the release only after every earlier step succeeds.

If a run fails after creating the draft, rerun the workflow. It may replace
assets on that draft, but it refuses to alter an already-published release.

The Arch/AUR recipe deliberately tracks the latest published release rather
than an unreleased source tree. After publishing, update `pkgver`, reset
`pkgrel` to 1, replace the source checksum, regenerate `.SRCINFO` with
`makepkg --printsrcinfo > .SRCINFO`, and test with `makepkg --verifysource`.
