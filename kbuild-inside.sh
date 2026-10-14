#!/bin/bash
# kbuild container entrypoint. Runs as root; the build itself runs as builder.
# Mounts: /pkg (ro, recipe) /sources (rw, SRCDEST cache) /out (rw, PKGDEST)
#         /repo (ro, local kashira repo) /kbuild (ro, this repo)
set -euo pipefail

KBUILD_JOBS=${KBUILD_JOBS:-8}
[[ "$KBUILD_JOBS" =~ ^[1-9][0-9]*$ ]] && [ "$KBUILD_JOBS" -le 16 ] || { echo "invalid KBUILD_JOBS: $KBUILD_JOBS" >&2; exit 2; }
MAKEPKG_CONF=${KBUILD_MAKEPKG_CONF:-makepkg.conf}
case "$MAKEPKG_CONF" in
  */*|.|..) echo "invalid KBUILD_MAKEPKG_CONF: $MAKEPKG_CONF" >&2; exit 2 ;;
esac
[ -f "/kbuild/conf/$MAKEPKG_CONF" ] || { echo "missing makepkg config: $MAKEPKG_CONF" >&2; exit 2; }

export PACKAGER="kashira build service <repo@kashiraproject.org>"
export LC_ALL=C.UTF-8
# merged-/usr: force libdir=lib for all cmake builds (see conf file).
# CMAKE_PROJECT_INCLUDE has no env-var form in cmake, so install a wrapper
# ahead of /usr/bin in PATH that injects it on configure runs.
install -Dm755 /kbuild/conf/cmake-wrapper.sh /usr/local/bin/cmake

# shellcheck disable=SC2015  # deliberate A&&B||C: -Sy only when asked, else -Syu
[ "${KBUILD_SKIP_UPGRADE:-0}" = 1 ] && pacman --config /kbuild/conf/pacman-kbuild.conf -Sy --noconfirm || pacman --config /kbuild/conf/pacman-kbuild.conf -Syu --noconfirm

# Deps come from the host (parsed via makepkg --printsrcinfo). Install ONLY
# from our repo: a failure here means a missing package in the distro.
# gcc is NOT force-installed any more: gcc-runtime carries the libstdc++
# headers and the GCC CRT objects clang needs for GCC-installation detection,
# so clang++ builds C++ in a base-devel image with no gcc present. Packages
# that genuinely need gcc declare it in makedepends, and that dep now decides.
# Overwrite stays scoped to KBUILD_OVERWRITE for packages whose recipe
# legitimately replaces a file (see note in the repo README).
pacman_args=(-S --needed --noconfirm)
[ -n "${KBUILD_OVERWRITE:-}" ] && pacman_args+=(--overwrite "$KBUILD_OVERWRITE")
if [ -n "${KBUILD_DEPS:-}" ]; then
  # shellcheck disable=SC2086  # KBUILD_DEPS is a space-separated dep list
  pacman --config /kbuild/conf/pacman-kbuild.conf "${pacman_args[@]}" $KBUILD_DEPS
fi
if [ -f /repo/.kbuild-arch-mixed ]; then
  pacman -Q > /out/.installed-packages
fi

id builder &>/dev/null || useradd -m builder
# KBUILD_KEEP_SRC: build into /work (a mounted rw volume) so failed build
# trees survive for inspection
if [ -n "${KBUILD_KEEP_SRC:-}" ]; then
  BUILDDIR=/work
else
  BUILDDIR=/home/builder/build
fi
mkdir -p "$BUILDDIR"
cp -a /pkg/. "$BUILDDIR/"
chown -R builder:builder "$BUILDDIR"

TEMP_CONF=$(mktemp)
trap 'rm -f "$TEMP_CONF"' EXIT
cat "/kbuild/conf/$MAKEPKG_CONF" >"$TEMP_CONF"
printf 'MAKEFLAGS="-j%s"\n' "$KBUILD_JOBS" >>"$TEMP_CONF"
chown builder:builder "$TEMP_CONF"

su builder -c "cd $BUILDDIR && \
  PKGDEST=/out SRCDEST=/sources BUILDDIR=$BUILDDIR \
  makepkg --config $TEMP_CONF -sf --noconfirm --skippgpcheck --nodeps"
