#!/usr/bin/env bash
# Build the claw-anything runner image for LoopAgent (trial-in-container).
# Usage: scripts/build_loop_image.sh [IMAGE_TAG]
#
# Creates a minimal build context in a temp dir — only code files are included.
# Large data directories (tasks/, benchmark/, build/, …) are never sent to
# the Docker daemon, keeping the build fast.
#
# Smallest of the three build scripts: no OpenHarness, no adb. LoopAgent runs
# claw-anything's native loop and needs nothing outside this repo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_TAG="${1:-claw-anything-loop:latest}"
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

# Dockerfile
cp "$REPO_ROOT/Dockerfile.loop" "$TMPCTX/"

echo "→ Context size: $(du -sh "$TMPCTX" | cut -f1)"

# ── Docker build ──────────────────────────────────────────────────────────────
echo "→ Building $IMAGE_TAG ..."
docker build \
    --build-arg REGISTRY="$REGISTRY" \
    -f "$TMPCTX/Dockerfile.loop" \
    -t "$IMAGE_TAG" \
    "$TMPCTX"

echo "✓ Done: $IMAGE_TAG"
echo "  Verify claw-anything: docker run --rm $IMAGE_TAG --help"
