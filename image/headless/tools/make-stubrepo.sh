#!/bin/sh
# Regenerate the empty stub packages in ../stubrepo.
#
# mkosi's bundled mkosi-initrd config (resources/mkosi-initrd/mkosi.conf.d/arch.conf)
# unconditionally installs btrfs-progs, xfsprogs, dosfstools and libfido2 on
# Distribution=arch. The kashira repo doesn't carry them and the headless
# image doesn't need them (ext4 rootfs, no LUKS/FIDO2, ESP fsck not done in
# initrd), so we satisfy the resolver with empty packages. If kashira starts
# shipping the real packages, the stub repo (listed last in
# mkosi.sandbox/etc/pacman.conf) loses precedence automatically.
set -e
cd "$(dirname "$0")/../stubrepo"
rm -f *.pkg.tar.zst kashira-mkosi-stubs.db* kashira-mkosi-stubs.files*

for p in btrfs-progs xfsprogs dosfstools libfido2; do
    d=$(mktemp -d)
    cat > "$d/.PKGINFO" <<EOF
pkgname = $p
pkgbase = kashira-mkosi-stubs
pkgver = 1-1
pkgdesc = Empty stub satisfying mkosi-initrd's Arch package list; not present in the kashira repo
url = https://github.com/kashira
builddate = 1
packager = kashira build
size = 0
arch = any
EOF
    (cd "$d" && bsdtar -cf "$OLDPWD/$p-1-1-any.pkg.tar.zst" .PKGINFO)
    rm -rf "$d"
done

repo-add kashira-mkosi-stubs.db.tar.gz ./*.pkg.tar.zst
