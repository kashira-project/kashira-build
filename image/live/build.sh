#!/usr/bin/env bash
# Build a Kashira live ISO with archiso (mkarchiso) inside a disposable container
# made from the package-owned base-devel image. archiso and its tools are
# installed from the isolated candidate repo; nothing is installed on the host.
#   image/live/build.sh [profile-dir]      (default: ./profile)
# Output: $KISO_OUT/kashira-*.iso plus build.log and the profile's package list.
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd)
profile=$(cd -- "${1:-$here/profile}" && pwd)
repo=${KISO_REPO:-/mnt/kashira/repos/v3}
image=${KISO_IMAGE:-kashira:base-devel-v3-20261023}
# Secure Boot signing keys (db.key/db.crt). Mounted read-only into the build
# container only; the private key never enters the ISO. KISO_SB_KEYS= disables
# signing (unsigned ISO, Secure Boot must then be off).
sb_keys=${KISO_SB_KEYS-/mnt/kashira/src/kashira/keys/secureboot}
out=${KISO_OUT:-/mnt/kashira/images/iso/$(basename -- "$profile")-$(date +%Y%m%d-%H%M%S)}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -f $profile/profiledef.sh && -f $profile/packages.x86_64 ]] || die "not an archiso profile: $profile"
[[ -f $repo/kashira.db.tar.gz ]] || die "no repo DB at $repo"
# The profile's pacman.conf must point at the same repo this run mounts.
grep -qx "Server = file://$repo" "$profile/pacman.conf" || die "profile pacman.conf does not use $repo"
[[ ! -e $out ]] || die "output exists: $out"
sb_args=()
if [[ -n $sb_keys ]]; then
    [[ -r $sb_keys/db.key && -r $sb_keys/db.crt ]] || die "Secure Boot keys not readable in $sb_keys"
    sb_args=(-v "$sb_keys/db.key:/keys/db.key:ro" -v "$sb_keys/db.crt:/keys/db.crt:ro"
             -e KASHIRA_SB_KEY=/keys/db.key -e KASHIRA_SB_CERT=/keys/db.crt)
fi
mkdir -p -- "$out"

# --privileged: pacstrap bind-mounts /proc,/sys,/dev into the airootfs.
docker run --rm --privileged --network none \
    -v "$repo:$repo:ro" \
    -v "$profile:/profile:ro" \
    -v "$out:/out" \
    "${sb_args[@]}" \
    "$image" bash -euo pipefail -c '
        cp /profile/pacman.conf /etc/pacman.conf
        pacman -Sy --noconfirm --needed --noprogressbar archiso sbsigntools openssl
        mkarchiso -v -r -w /work -o /out /profile
        ls /out/*.iso
    ' > "$out/build.log" 2>&1 || die "mkarchiso failed; see $out/build.log"

iso=$(ls -- "$out"/*.iso)
(cd -- "$out" && sha256sum -- "${iso##*/}") > "$iso.sha256"
# run under sudo; the boot test writes next to the ISO as the invoking user
[ -z "${SUDO_UID:-}" ] || chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" -- "$out"
printf '%s\n' "$iso"
