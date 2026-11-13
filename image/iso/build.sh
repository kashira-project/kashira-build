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
        # A 10 s network stall makes pacman drop a file for good and aborts mkarchiso; fetch everything into
        # the cache first, in a retry loop (the pacstrap -c of mkarchiso then installs from this cache).
        for try in 1 2 3 4 5 6 7 8; do
            pacman -Sw --noconfirm --noprogressbar $(grep -v "^#" /profile/packages.x86_64) && break
            [ "$try" != 8 ] || exit 1
            echo "prefetch attempt $try failed, retrying"; sleep 20
        done
        /archiso/mkarchiso -v -r -w /work -o /out /profile
        ls /out/*.iso
    ' > "$out/build.log" 2>&1 || die "mkarchiso failed; see $out/build.log"

iso=$(ls -- "$out"/*.iso)
# Ventoy normal mode (UEFI) only finds an EFI El Torito image placed after a section header (91 EF), as on
# BIOS+UEFI media. A UEFI-only catalog has just validation(01 EF)+default entry, so Ventoy never swaps in its
# initrd and archiso stops at "Device <uuid> not found". Append a final EFI section repeating the default entry.
python3 -I - "$iso" <<'EOF' || die "El Torito catalog patch failed"
import struct, sys
with open(sys.argv[1], 'r+b') as f:
    f.seek(17 * 2048)
    brvd = f.read(2048)
    assert brvd[1:6] == b'CD001' and brvd[7:30].rstrip(b'\0') == b'EL TORITO SPECIFICATION', 'no El Torito'
    off = struct.unpack_from('<I', brvd, 71)[0] * 2048
    f.seek(off)
    cat = bytearray(f.read(128))
    assert cat[0:2] == b'\x01\xef' and cat[32] == 0x88 and cat[64:128] == bytes(64), 'unexpected catalog layout'
    cat[64:96] = b'\x91\xef\x01\x00' + bytes(28)
    cat[96:128] = cat[32:64]
    f.seek(off)
    f.write(cat)
EOF
(cd -- "$out" && sha256sum -- "${iso##*/}") > "$iso.sha256"
[ -z "${SUDO_UID:-}" ] || chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" -- "$out"
printf '%s\n' "$iso"
