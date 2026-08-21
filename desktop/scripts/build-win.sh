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
# deploy/prod/publish-r2.sh.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
desktop_dir="$(dirname "$here")"
tree="$desktop_dir/build/upstream"
image="electronuserland/builder:wine"

# ⚠️ 必须在 **x86_64** 主机上跑。该镜像只有 linux/amd64, 在 Apple Silicon 上
# Docker 会用 qemu 模拟, 而 wine 在模拟层下必崩 (2026-08-20 实测):
#   wine: dlls/ntdll/unix/virtual.c: anon_mmap_fixed:
#         Assertion `!((UINT_PTR)start & host_page_mask)' failed.
#   qemu: uncaught target signal 6 (Aborted) - core dumped
# 崩在 NSIS 打包阶段, 前面的 yarn build 全绿, 所以看着像"构建完了没产物"。
# 在 arm64 Mac 上直接拦下, 别浪费一轮镜像拉取和构建。
if [ "$(uname -m)" != "x86_64" ] && [ "${ALLOW_EMULATED_WINE:-0}" != "1" ]; then
  echo "!! 本机是 $(uname -m); $image 只有 amd64, qemu 模拟下 wine 会崩。" >&2
  echo "   在 x86_64 Linux 主机上跑本脚本, 或用 CI 的 windows job。" >&2
  echo "   (确知要试模拟可 ALLOW_EMULATED_WINE=1 覆盖)" >&2
  exit 1
fi

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
# ⚠️ 容器里跑的脚本必须**单引号 heredoc** (<<'INNER') 传进来, 不能直接写在
# bash -lc "..." 的双引号里 —— 2026-08-20 踩到: 里面一句注释写了
# yarn install 并用反引号括起来, 而双引号内的反引号会被**宿主**当命令替换先执行掉,
# 于是 docker run 根本没跑, 宿主莫名其妙跑了一遍 yarn, 替换结果又被当成命令
# 报 "info: command not found", set -e 就地退出 —— 全程看着像"构建完成了但
# 没有产物"。单引号 heredoc 内一切照字面传递, 变量则显式用 -e 注入。
IFS='' read -r -d '' INNER_SCRIPT <<'INNER' || true
set -euo pipefail
corepack enable
if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  echo "--- yarn install"
  yarn install --immutable || yarn install
fi
echo "--- yarn build (plugin sources -> lib/)"
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
# to the correct Windows prebuild. (Restore it by re-running yarn install
# before building for mac or linux from this same tree.)
rm -rf node_modules/node-pty/build/Release ../node_modules/node-pty/build/Release 2>/dev/null || true
echo "--- electron-builder --win nsis x64,arm64"
# Invoke electron-builder directly: upstream's dist:win wrapper hard-fails
# on non-Windows hosts by design, and it only knows about x64.
yarn electron-builder --win nsis --x64 --arm64 --publish never --config.npmRebuild=false
INNER
# BUILD_MEMORY 给容器设内存上限 (如 4g)。这个构建常在**同时跑着生产服务**的机器
# 上做 —— electron-builder 打两个架构会吃掉好几个 G, 不设限就可能把同机的数据库
# 或工作台挤出去 (那台机器的内存压力有案底)。不设则不限制。
docker run --rm ${BUILD_MEMORY:+--memory "$BUILD_MEMORY" --memory-swap "$BUILD_MEMORY"} \
  -v "$tree:/project" \
  -v "$cache_dir:/root/.cache/electron-builder" \
  -w /project \
  -e ELECTRON_CACHE=/root/.cache/electron-builder \
  -e CSC_IDENTITY_AUTO_DISCOVERY=false \
  -e SKIP_INSTALL="$skip_install" \
  "$image" bash -lc "$INNER_SCRIPT"

echo "==> artifacts"
dist="$tree/dsh-plugin-desktop/dist"
# Building two arches also produces a combined dual-arch installer (~291MB).
# Name the two we actually ship rather than globbing *.exe: the glob neither
# verified nor excluded the combined build, so "some .exe exists" was passing
# for a success check even if a per-arch one was missing.
# 版本号从装配树的 package.json 读, **不要写死** —— assemble 会按
# runtimePackageVersion 派生对外版本 (0.1.0-rc.6 -> 0.1.6), 写死的话版本一变
# 产物名就对不上, 于是三个 exe 明明都打出来了, 脚本却在最后一步报
# "missing expected artifact" 退出 (2026-08-20 实测: 版本从 2.0.0 改成 0.1.6
# 当天就踩到)。这正是加版本派生时写下的那句"版本号写死在两处就一定会漂"。
ver="$(node -p "require('$tree/dsh-plugin-desktop/package.json').version" 2>/dev/null)"
[ -n "$ver" ] || { echo "!! 读不出装配树的版本号, 先跑 assemble.mjs" >&2; exit 1; }
echo "    版本: $ver"
x64_exe="$dist/DSH-Cloud-Desktop-$ver-x64-Setup.exe"
arm_exe="$dist/DSH-Cloud-Desktop-$ver-arm64-Setup.exe"
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
echo "  deploy/prod/publish-r2.sh"
