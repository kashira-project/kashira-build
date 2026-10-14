#!/usr/bin/env bash
# Build a Kashira live ISO with archiso (mkarchiso) inside a disposable container
# made from the package-owned base-devel image. archiso and its tools are
# installed from the isolated candidate repo; nothing is installed on the host.
#   image/live/build.sh [profile-dir]      (default: ./profile)
# Output: $KISO_OUT/kashira-*.iso plus build.log and the profile's package list.
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd)
profile=$(cd -- "${1:-$here/profile}" && pwd)
repo=${KISO_REPO:-/mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/repo}
image=${KISO_IMAGE:-kashira:base-devel-takeover-20261003}
out=${KISO_OUT:-/mnt/lfs/tmp/kashira-iso/$(basename -- "$profile")-$(date +%Y%m%d-%H%M%S)}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -f $profile/profiledef.sh && -f $profile/packages.x86_64 ]] || die "not an archiso profile: $profile"
[[ -f $repo/kashira.db.tar.gz ]] || die "no repo DB at $repo"
# The profile's pacman.conf must point at the same repo this run mounts.
grep -qx "Server = file://$repo" "$profile/pacman.conf" || die "profile pacman.conf does not use $repo"
[[ ! -e $out ]] || die "output exists: $out"
mkdir -p -- "$out"

# --privileged: pacstrap bind-mounts /proc,/sys,/dev into the airootfs.
docker run --rm --privileged --network none \
    -v "$repo:$repo:ro" \
    -v "$profile:/profile:ro" \
    -v "$out:/out" \
    "$image" bash -euo pipefail -c '
        cp /profile/pacman.conf /etc/pacman.conf
        pacman -Sy --noconfirm --needed --noprogressbar archiso
        mkarchiso -v -r -w /work -o /out /profile
        ls /out/*.iso
    ' > "$out/build.log" 2>&1 || die "mkarchiso failed; see $out/build.log"

iso=$(ls -- "$out"/*.iso)
sha256sum -- "$iso" > "$iso.sha256"
printf '%s\n' "$iso"
