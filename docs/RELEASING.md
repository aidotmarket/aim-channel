# Releasing AIM Data

## Overview

AIM Data releases use `scripts/release-aim-data.sh` to create namespaced Git tags and `.github/workflows/aim-data-release.yml` to build, verify, and publish the customer image and GitHub Release.

Do not manually create release tags or publish Docker images. Use the release script from the AIM Data repository.

## Prerequisites

- Work from `/Users/max/Projects/ai-market/aim-data` on `main` with a clean working tree.
- Ensure local `main` contains the release changes.
- Install and authenticate the `gh` CLI.
- Start Docker Desktop or OrbStack and ensure the Docker CLI can reach GHCR. The script uses `GITHUB_TOKEN` or the configured Doppler fallback if GHCR login is required.

## Version defaults

The release script is the single update path for the customer-facing default version. Its `update_release_defaults` function rewrites these three embedded defaults together:

- `docker-compose.aim-data.yml`
- `installers/aim-data/install.sh`
- `installers/aim-data/install.ps1`

The installers keep an embedded default because each installer can be downloaded and run as a standalone file; neither can depend on a separate version file being downloaded first.

Release candidates do not change customer defaults. Promotion updates all three defaults to the stable version in one commit. `.github/workflows/ci-release-integrity.yml` fails if any default differs from the latest stable `aim-data-vX.Y.Z` tag.

## Create a release candidate

From the repository root:

```bash
./scripts/release-aim-data.sh rc patch
./scripts/release-aim-data.sh rc minor
./scripts/release-aim-data.sh rc major
```

The script finds the latest stable `aim-data-vX.Y.Z` tag, calculates the next semantic version and RC number, creates an annotated `aim-data-vX.Y.Z-rc.N` tag, and pushes it. It also creates the GitHub prerelease entry.

The tag triggers `.github/workflows/aim-data-release.yml`. The workflow builds and pushes `ghcr.io/aidotmarket/aim-data:vX.Y.Z-rc.N`, verifies its published `version` label, checks its multi-architecture manifest, runs the container health smoke test, and attaches the installers and compose file to the GitHub prerelease.

## Promote an RC to stable

Promote the latest RC:

```bash
./scripts/release-aim-data.sh promote
```

Or select an RC explicitly:

```bash
./scripts/release-aim-data.sh promote aim-data-v1.22.4-rc.2
```

Promotion removes the `-rc.N` suffix, updates the compose and both installer defaults together, commits that change, and atomically pushes `main` with an annotated stable tag such as `aim-data-v1.22.4`. The atomic push ensures the strict latest-stable CI check can see the matching tag when it validates the commit.

The stable tag triggers the release workflow. The workflow builds and pushes both `ghcr.io/aidotmarket/aim-data:v1.22.4` and `ghcr.io/aidotmarket/aim-data:latest`; it does not promote the RC image by retagging it. After the push, it pulls the published stable image and fails unless the image's `version` label exactly equals `v1.22.4`. A stable label containing `-rc.` also fails explicitly. The GitHub Release is created only after that proof and the smoke test pass.

A cold multi-architecture build includes the LibreOffice, Tesseract, and Torch layers under QEMU and can take up to 90 minutes. After the atomic push, `promote` waits for the stable version manifest to become pullable, locates the release workflow run for the exact stable tag and commit, and watches it to a successful terminal state before reporting success. This means the published image label, multi-architecture manifest, container health smoke test, and GitHub Release have all passed; bare image existence alone is not treated as success. If the image does not appear before the timeout, the matching workflow run is absent, or the workflow fails, the command exits non-zero with recovery guidance; do not leave the pushed defaults on `main` pointing at an unverified image, move the tag, or publish an image manually.

During the promotion window, which can take up to 90 minutes for a cold build, a new customer's install fails cleanly at the Docker pull with a clear error and is safely re-runnable once the image is published.

`Dockerfile.customer` materializes the `VERSION` build argument in `/etc/aim-data-version` before applying the image label and environment value. A version change therefore invalidates the runtime layer even when the workflow imports the GitHub Actions BuildKit cache.

## Workflow checks

`.github/workflows/aim-data-release.yml` performs these jobs:

1. `build-push` extracts the version from the `aim-data-v*` tag, builds the AMD64 and ARM64 image, pushes the release tag (and `latest` for stable releases), and verifies the published image label.
2. `smoke-test` verifies both architectures are present, pulls the published image, starts it, and waits for `/api/health`.
3. `create-release` creates or updates the GitHub Release and attaches `install.sh`, `install.ps1`, and `docker-compose.aim-data.yml` only after the preceding jobs pass.

`.github/workflows/ci-release-integrity.yml` runs on relevant pushes to `main`. It fails closed when the compose, shell-installer, or PowerShell-installer default does not match the latest stable tag. It also checks the multi-architecture workflow configuration, shell syntax, installer safety conventions, and the `v` prefix.

## Rollback

There is currently no automated rollback path. The previous rollback workflow was removed because it published from the wrong registry namespace and bypassed release verification. Rollback today means promoting an earlier stable version through the normal release path.

## Verification and recovery

List recent release runs:

```bash
gh run list --workflow=aim-data-release.yml --limit 5
```

Inspect a failed run before taking any recovery action:

```bash
gh run view <run-id> --log-failed
```

If a release workflow fails, do not overwrite or move the existing tag and do not manually publish an image. Fix the cause on `main`, then use the normal release script to create a new RC or stable version. The stable GitHub Release is not created unless the image-label proof and smoke test pass.

GHCR image tags always include the `v` prefix (`v1.22.4`, not `1.22.4`), while Git tags include the repository namespace (`aim-data-v1.22.4`).
