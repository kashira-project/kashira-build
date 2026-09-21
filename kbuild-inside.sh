#!/bin/bash
# kbuild container entrypoint. Runs as root; the build itself runs as builder.
# Mounts: /pkg (ro, recipe) /sources (rw, SRCDEST cache) /out (rw, PKGDEST)
#         /repo (ro, local kashira repo) /kbuild (ro, this repo)
set -euo pipefail

export PACKAGER="kashira build service <repo@kashiraproject.org>"
export LC_ALL=C.UTF-8

[ "${KBUILD_SKIP_UPGRADE:-0}" = 1 ] && pacman --config /kbuild/conf/pacman-kbuild.conf -Sy --noconfirm || pacman --config /kbuild/conf/pacman-kbuild.conf -Syu --noconfirm

# Deps come from the host (parsed via makepkg --printsrcinfo). Install ONLY
# from our repo: a failure here means a missing package in the distro.
if [ -n "${KBUILD_DEPS:-}" ]; then
  pacman --config /kbuild/conf/pacman-kbuild.conf -S --needed --noconfirm $KBUILD_DEPS
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

su builder -c "cd $BUILDDIR && \
  PKGDEST=/out SRCDEST=/sources BUILDDIR=$BUILDDIR
  MAKEFLAGS='-j$(nproc)' \
  makepkg --config /kbuild/conf/${KBUILD_MAKEPKG_CONF:-makepkg.conf} -sf --noconfirm --skippgpcheck --nodeps"
