#!/usr/bin/env bash
# Build the Windows installers (x64 + arm64) on this machine, in Docker.
#
# Upstream's scripts/package-win.ts refuses to run anywhere but a native Windows
# host. We have no Windows host, and CI minutes are not free — so the build runs
# in electronuserland/builder:wine, which carries the Node 24 + Yarn 4 + Wine 11
# combination electron-builder needs to produce NSIS installers off-Windows.
#
#   ./desktop/scripts/build-win.sh [--skip-install]
#
# Prerequisite: desktop/build/upstream must already be assembled
#   node desktop/scripts/assemble.mjs
#
# Artifacts land in desktop/build/upstream/dsh-plugin-desktop/dist/ and are
# verified before the script reports success. Publish them with
# deploy/open-search/publish-r2.sh.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
desktop_dir="$(dirname "$here")"
tree="$desktop_dir/build/upstream"
image="electronuserland/builder:wine"

[ -d "$tree/dsh-plugin-desktop" ] || {
  echo "desktop/build/upstream is not assembled. Run:" >&2
  echo "  node $here/assemble.mjs" >&2
  exit 1
}

skip_install=0
[ "${1:-}" = "--skip-install" ] && skip_install=1

# electron-builder caches ~150MB of Electron and winCodeSign per arch. Keeping
# the cache on the host makes a rebuild minutes instead of a fresh download, and
# keeps it out of the assembled tree (which assemble.mjs wipes).
cache_dir="$desktop_dir/.cache-electron"
mkdir -p "$cache_dir"

echo "==> building Windows x64 + arm64 in $image"
docker run --rm \
  -v "$tree:/project" \
  -v "$cache_dir:/root/.cache/electron-builder" \
  -w /project \
  -e ELECTRON_CACHE=/root/.cache/electron-builder \
  -e CSC_IDENTITY_AUTO_DISCOVERY=false \
  "$image" bash -lc "
    set -euo pipefail
    corepack enable
    if [ '$skip_install' != '1' ]; then
      echo '--- yarn install'
      yarn install --immutable || yarn install
    fi
    echo '--- yarn build (plugin sources -> lib/)'
    cd dsh-plugin-desktop
    yarn build
    # node-pty is the only native dependency, and it already ships prebuilt
    # binaries for win32-x64 AND win32-arm64. @electron/rebuild does not know
    # that and tries to compile from source, which node-gyp cannot cross-compile
    # — that is the whole reason upstream restricts this build to Windows hosts.
    # Skipping the rebuild uses the shipped prebuilds instead.
    #
    # One trap: node-pty resolves build/Release BEFORE prebuilds/<platform>-<arch>,
    # and on this host build/Release holds the Linux ELF from yarn install. Ship
    # that and the app dies on launch. Removing it makes the runtime fall through
    # to the correct Windows prebuild. (Restore it with `yarn install` before
    # building for mac or linux from this same tree.)
    rm -rf node_modules/node-pty/build/Release ../node_modules/node-pty/build/Release 2>/dev/null || true
    echo '--- electron-builder --win nsis x64,arm64'
    # Invoke electron-builder directly: upstream's dist:win wrapper hard-fails
    # on non-Windows hosts by design, and it only knows about x64.
    yarn electron-builder --win nsis --x64 --arm64 --publish never --config.npmRebuild=false
  "

echo
echo "==> artifacts"
dist="$tree/dsh-plugin-desktop/dist"
# Building two arches also produces a combined dual-arch installer (~291MB).
# Name the two we actually ship rather than globbing *.exe: the glob neither
# verified nor excluded the combined build, so "some .exe exists" was passing
# for a success check even if a per-arch one was missing.
x64_exe="$dist/DSH-Cloud-Desktop-2.0.0-x64-Setup.exe"
arm_exe="$dist/DSH-Cloud-Desktop-2.0.0-arm64-Setup.exe"
for exe in "$x64_exe" "$arm_exe"; do
  [ -f "$exe" ] || { echo "missing expected artifact: $exe" >&2; exit 1; }
  ls -lh "$exe"
done

echo
echo "==> verify each packaged tree carries the cloud login assets"
# The verifier walks a packaging output DIRECTORY (it reads app.asar headers and
# unpacked build/cloud); the .exe is an NSIS wrapper it cannot see into. Each
# arch produces its own unpacked tree, and both must be checked — a per-arch
# packaging step can drop assets for one arch and not the other.
found=0
for tree_dir in "$dist"/win-unpacked "$dist"/win-arm64-unpacked; do
  [ -d "$tree_dir" ] || continue
  found=$((found + 1))
  node "$here/verify-package.mjs" "$tree_dir" || exit 1

  # And prove the native module is the Windows one. A Linux .node inside a
  # Windows build passes every source-level check and then crashes on launch.
  case "$tree_dir" in
    *arm64*) want="win32-arm64" ;;
    *)       want="win32-x64" ;;
  esac
  pty="$(find "$tree_dir" -path "*node-pty/prebuilds/$want/pty.node" | head -1)"
  [ -n "$pty" ] || { echo "  MISSING node-pty prebuild for $want in $tree_dir" >&2; exit 1; }
  if file "$pty" | grep -q ELF; then
    echo "  $pty is an ELF binary — a Linux build leaked into the Windows package" >&2
    exit 1
  fi
  echo "  OK  node-pty $want -> $(file -b "$pty" | cut -c1-60)"
  if find "$tree_dir" -path "*node-pty/build/Release/*.node" | grep -q .; then
    echo "  build/Release survived; it shadows the correct prebuild at runtime" >&2
    exit 1
  fi
done
[ "$found" -eq 2 ] || {
  echo "expected an unpacked tree per arch, found $found" >&2
  exit 1
}

# publish-r2.sh uploads whatever sits in the server's data volume, NOT this
# dist/ directory. Without this step a fresh build stays local and the publisher
# silently re-uploads the previous release: that is exactly how win-x64 shipped
# a build 17 commits behind while win-arm64 was current, both under 2.0.0.
if docker inspect dhc-server >/dev/null 2>&1; then
  echo
  echo "==> stage into the server data volume (what publish-r2.sh reads)"
  for exe in "$x64_exe" "$arm_exe"; do
    docker cp "$exe" dhc-server:/app/data/releases/
    echo "    staged $(basename "$exe")"
  done
else
  echo
  echo "NOTE: dhc-server not running; artifacts left in $dist."
  echo "      Stage them into /app/data/releases before publishing."
fi

echo
echo "Done. Publish with:"
echo "  deploy/open-search/publish-r2.sh"
