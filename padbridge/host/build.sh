#!/bin/bash
# Make a self-contained release: dist/padbridge-host-<version>.tar.gz with the engine vendored in engine/.
#   ./build.sh [ENGINE_DIR]     (default: ../../ds4-bt-bridge)
# The engine is copied, never modified. install.sh in the tarball uses that copy.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENGINE=${1:-$HERE/../../ds4-bt-bridge}
VERSION=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import padbridge; print(padbridge.VERSION)' "$HERE")
NAME=padbridge-host-$VERSION
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME/engine"
cp -r "$HERE/padbridge" "$HERE/bin" "$HERE/data" "$HERE/mock" "$HERE/tests" "$STAGE/$NAME/"
cp "$HERE/install.sh" "$HERE/uninstall.sh" "$HERE/README.md" "$STAGE/$NAME/"
for f in ds4_bt_bridge.py bridge_linux.py ds4_bt_setup.sh; do
  cp "$ENGINE/$f" "$STAGE/$NAME/engine/"
done
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
( cd "$ENGINE" && shasum -a 256 ds4_bt_bridge.py bridge_linux.py ds4_bt_setup.sh ) > "$STAGE/$NAME/engine/SHA256SUMS"
mkdir -p "$HERE/dist"
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -C "$STAGE" -czf "$HERE/dist/$NAME.tar.gz" "$NAME"
echo "dist/$NAME.tar.gz"
