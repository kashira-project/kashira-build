#!/usr/bin/env bash
# Build the Kashira live ISO: the vendored archiso (archiso/, upstream v92) runs in a disposable container from
# the base-devel image, and every package comes from R2 (profile/pacman.conf). Nothing local is used.
#   image/iso/build.sh [profile-dir]      (default: ./profile)
# Output: $KISO_OUT/kashira-*.iso, its .sha256 and build.log.
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd)
profile=$(cd -- "${1:-$here/profile}" && pwd)
image=${KISO_IMAGE:-kashira:base-devel-v3-20261023}
out=${KISO_OUT:-/mnt/kashira/images/iso/$(basename -- "$profile")-$(date +%Y%m%d-%H%M%S)}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
[[ -f $profile/profiledef.sh && -f $profile/packages.x86_64 ]] || die "not an archiso profile: $profile"
[[ ! -e $out ]] || die "output exists: $out"
mkdir -p -- "$out"

# --privileged: pacstrap bind-mounts /proc,/sys,/dev into the airootfs. -Syu first, so the container's
# pacman and archiso tools match R2 (an old pacman skips scriptlets after hostname downloads; pacman 7.1.0-8).
docker run --rm --privileged \
    -v "$here/archiso:/archiso:ro" \
    -v "$profile:/profile:ro" \
    -v "$out:/out" \
    "$image" bash -euo pipefail -c '
        cp /profile/pacman.conf /etc/pacman.conf
        pacman -Syu --noconfirm --needed --noprogressbar arch-install-scripts dosfstools e2fsprogs erofs-utils \
            libarchive libisoburn mtools squashfs-tools
        /archiso/mkarchiso -v -r -w /work -o /out /profile
        ls /out/*.iso
    ' > "$out/build.log" 2>&1 || die "mkarchiso failed; see $out/build.log"

iso=$(ls -- "$out"/*.iso)
(cd -- "$out" && sha256sum -- "${iso##*/}") > "$iso.sha256"
[ -z "${SUDO_UID:-}" ] || chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" -- "$out"
printf '%s\n' "$iso"
