#!/usr/bin/env bash
# Build the claw-anything runner image for OpenHarnessExtended (trial-in-container).
# Usage: scripts/build_oh_ext_image.sh [IMAGE_TAG]
#
# Creates a minimal build context in a temp dir — only code files are included.
# Large data directories (tasks/, benchmark/, node_modules/, …) are never sent
# to the Docker daemon, keeping the build fast.
#
# OpenHarnessExtended source and the adb binary live outside this repo. Override
# their locations via the OH_EXT_DIR / ADB_PATH environment variables, or leave
# OH_EXT_DIR unset to have this script auto-clone OpenHarnessExtended into
# ``vendor/OpenHarnessExtended`` (gitignored).
set -euo pipefail

# ── User-overridable paths (leave blank to use defaults / auto-clone) ─────────
# Override on the command line if you have a working copy in a non-standard
# location, e.g.:
#   OH_EXT_DIR=$HOME/code/OpenHarnessExtended ADB_PATH=$HOME/tools/platform-tools/adb \
#       scripts/build_oh_ext_image.sh
OH_EXT_DIR="${OH_EXT_DIR:-}"
ADB_PATH="${ADB_PATH:-}"

# ── Internal config ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_TAG="${1:-claw-anything-oh-ext:latest}"
REGISTRY="${REGISTRY:-docker.m.daocloud.io}"

#: Where this script will auto-clone OpenHarnessExtended when OH_EXT_DIR is
#: empty. ``vendor/`` is the common OSS convention for vendored external source
#: (Go, Ruby, PHP); should be in .gitignore so the clone never lands in this
#: repo's history.
OH_EXT_CLONE_URL="https://github.com/LiberCoders/OpenHarnessExtended.git"
OH_EXT_VENDOR_DIR="$REPO_ROOT/vendor/OpenHarnessExtended"

# ── Resolve OH_EXT_DIR (env override → vendor clone → fatal) ─────────────────
if [[ -z "$OH_EXT_DIR" ]]; then
    if [[ -d "$OH_EXT_VENDOR_DIR/.git" ]]; then
        echo "→ Using existing vendor clone: $OH_EXT_VENDOR_DIR"
        OH_EXT_DIR="$OH_EXT_VENDOR_DIR"
    else
        echo "→ OH_EXT_DIR unset — cloning $OH_EXT_CLONE_URL into $OH_EXT_VENDOR_DIR"
        mkdir -p "$(dirname "$OH_EXT_VENDOR_DIR")"
        if git clone --depth=1 "$OH_EXT_CLONE_URL" "$OH_EXT_VENDOR_DIR"; then
            OH_EXT_DIR="$OH_EXT_VENDOR_DIR"
        else
            cat >&2 <<EOF
ERROR: Failed to clone $OH_EXT_CLONE_URL into $OH_EXT_VENDOR_DIR.

If you already have OpenHarnessExtended checked out somewhere, point this
script at it via the OH_EXT_DIR environment variable:

    OH_EXT_DIR=/path/to/OpenHarnessExtended scripts/build_oh_ext_image.sh

Otherwise check your network / git credentials and re-run.
EOF
            exit 1
        fi
    fi
fi

# ── Resolve ADB_PATH (env override → fatal with download hint) ───────────────
if [[ -z "$ADB_PATH" ]]; then
    cat >&2 <<EOF
ERROR: ADB_PATH is not set.

The image bundles a host-provided adb binary so we get the same version inside
and outside the container — there is no apt-get install layer. Download the
Android platform-tools, point ADB_PATH at the extracted adb binary, and re-run:

    Official download: https://developer.android.com/tools/releases/platform-tools

Example:

    ADB_PATH=\$HOME/Android/platform-tools/adb scripts/build_oh_ext_image.sh
EOF
    exit 1
fi

# ── Validate sources ─────────────────────────────────────────────────────────
for path in "$OH_EXT_DIR" "$ADB_PATH"; do
    if [[ ! -e "$path" ]]; then
        echo "ERROR: not found: $path" >&2
        exit 1
    fi
done

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

# OH-Ext: source files only, skip node_modules / build / benchmark / assets
mkdir -p "$TMPCTX/oh-ext/frontend/terminal"
cp    "$OH_EXT_DIR/pyproject.toml"                       "$TMPCTX/oh-ext/"
cp    "$OH_EXT_DIR/README.md"                            "$TMPCTX/oh-ext/"
cp -r "$OH_EXT_DIR/src"                                  "$TMPCTX/oh-ext/src"
cp -r "$OH_EXT_DIR/ohmo"                                 "$TMPCTX/oh-ext/ohmo"
cp    "$OH_EXT_DIR/frontend/terminal/package.json"       "$TMPCTX/oh-ext/frontend/terminal/"
cp    "$OH_EXT_DIR/frontend/terminal/tsconfig.json"      "$TMPCTX/oh-ext/frontend/terminal/"
cp -r "$OH_EXT_DIR/frontend/terminal/src"                "$TMPCTX/oh-ext/frontend/terminal/src"

# adb binary
cp "$ADB_PATH" "$TMPCTX/adb"

# Dockerfile
cp "$REPO_ROOT/Dockerfile.oh_ext" "$TMPCTX/"

echo "→ Context size: $(du -sh "$TMPCTX" | cut -f1)"

# ── Docker build ──────────────────────────────────────────────────────────────
echo "→ Building $IMAGE_TAG ..."
docker build \
    --build-arg REGISTRY="$REGISTRY" \
    -f "$TMPCTX/Dockerfile.oh_ext" \
    -t "$IMAGE_TAG" \
    "$TMPCTX"

echo "✓ Done: $IMAGE_TAG"
echo "  Verify claw-anything: docker run --rm $IMAGE_TAG --help"
echo "  Verify oh:            docker run --rm --entrypoint oh  $IMAGE_TAG --help"
echo "  Verify adb:           docker run --rm --entrypoint adb $IMAGE_TAG version"
