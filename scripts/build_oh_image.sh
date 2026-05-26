#!/usr/bin/env bash
# Build the claw-anything runner image for vanilla OpenHarness (trial-in-container).
# Usage: scripts/build_oh_image.sh [IMAGE_TAG]
#
# Creates a minimal build context in a temp dir — only code files are included.
# Large data directories (tasks/, benchmark/, build/, …) are never sent to
# the Docker daemon, keeping the build fast.
#
# Unlike build_oh_ext_image.sh, this script does NOT need OH_EXT_DIR / ADB_PATH:
# vanilla openharness-ai is pip-installed from PyPI in the Dockerfile, and the
# image carries no Android tooling.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_TAG="${1:-claw-anything-oh:latest}"
REGISTRY="${REGISTRY:-docker.m.daocloud.io}"

# ── Build context ─────────────────────────────────────────────────────────────
TMPCTX=$(mktemp -d)
trap 'rm -rf "$TMPCTX"' EXIT
echo "→ Preparing build context: $TMPCTX"

# claw-anything: code only, no tasks / benchmark / build / data dirs
mkdir -p "$TMPCTX/claw-anything"
cp    "$REPO_ROOT/pyproject.toml"   "$TMPCTX/claw-anything/"
cp    "$REPO_ROOT/requirements.txt" "$TMPCTX/"
cp -r "$REPO_ROOT/src"              "$TMPCTX/claw-anything/src"
cp -r "$REPO_ROOT/mock_services"    "$TMPCTX/claw-anything/mock_services"

# Build-time patch scripts — each rewrites one line of openharness-ai 0.1.9
# at image-build time. See Dockerfile.oh for the invocation order and each
# patch script's docstring for the upstream line it edits.
mkdir -p "$TMPCTX/oh"
cp "$REPO_ROOT"/docker/oh/patch_*.py "$TMPCTX/oh/"

# Dockerfile
cp "$REPO_ROOT/Dockerfile.oh" "$TMPCTX/"

echo "→ Context size: $(du -sh "$TMPCTX" | cut -f1)"

# ── Docker build ──────────────────────────────────────────────────────────────
echo "→ Building $IMAGE_TAG ..."
docker build \
    --build-arg REGISTRY="$REGISTRY" \
    -f "$TMPCTX/Dockerfile.oh" \
    -t "$IMAGE_TAG" \
    "$TMPCTX"

echo "✓ Done: $IMAGE_TAG"
echo "  Verify claw-anything: docker run --rm $IMAGE_TAG --help"
echo "  Verify oh:            docker run --rm --entrypoint oh $IMAGE_TAG --help"
