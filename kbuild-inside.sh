#!/bin/bash
# kbuild container entrypoint. Runs as root; the build itself runs as builder.
# Mounts: /pkg (ro, recipe) /sources (rw, SRCDEST cache) /out (rw, PKGDEST)
#         /repo (ro, local kashira repo) /kbuild (ro, this repo)
set -euo pipefail

export PACKAGER="kashira build service <repo@kashiraproject.org>"

pacman --config /kbuild/conf/pacman-kbuild.conf -Sy --noconfirm >/dev/null

# Deps come from the host (parsed via makepkg --printsrcinfo). Install ONLY
# from our repo: a failure here means a missing package in the distro.
if [ -n "${KBUILD_DEPS:-}" ]; then
  pacman --config /kbuild/conf/pacman-kbuild.conf -S --needed --noconfirm $KBUILD_DEPS
fi

id builder &>/dev/null || useradd -m builder
mkdir -p /home/builder/build
cp -a /pkg/. /home/builder/build/
chown -R builder:builder /home/builder/build

cd /home/builder/build
su builder -c "cd /home/builder/build && \
  PKGDEST=/out SRCDEST=/sources BUILDDIR=/home/builder/build \
  MAKEFLAGS='-j$(nproc)' \
  makepkg -sf --noconfirm --skippgpcheck --nodeps"
