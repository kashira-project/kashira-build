# kashira headless image (mkosi)

Bootable kashira disk image: systemd init, kashira `linux` kernel as a UKI
signed with the Secure Boot db key, systemd-boot in the ESP, ext4 rootfs.

## Build

Host needs: `mkosi`, `systemd-ukify`, `sbsigntools` (and `qemu-base`,
`edk2-ovmf` for boot testing).

```sh
cd /mnt/lfs/src/kashira-build/image/headless
sudo mkosi build            # add --force to rebuild over existing output
```

Artifacts (next to this file):

- `kashira-headless.raw` — bootable GPT disk image (ESP + root-x86-64), 4.2G
- `kashira-headless.efi` / `.vmlinuz` / `.initrd` — split UKI/kernel/initrd
  outputs (same content as what's in the ESP)

## How it works

- `Distribution=arch`; mkosi runs the host's pacman in a sandbox.
- `mkosi.sandbox/etc/pacman.conf` is copied into that sandbox. mkosi leaves a
  pre-existing sandbox pacman.conf untouched, so it is the single source of
  truth: the unsigned local kashira repo
  (`file:///mnt/lfs/var/cache/pacman/kashira`), plus the stub repo below.
  `ExtraSearchPaths=` in mkosi.conf bind-mounts both repo dirs into the
  sandbox (the sandbox otherwise only sees /usr, /etc, /home, ...).
- UKI + systemd-boot are signed by mkosi with
  `/mnt/lfs/src/kashira/keys/secureboot/db.{key,crt}` (sbsign). The ESP also
  carries `loader/keys/auto/{PK,KEK,db}.auth`, so firmware in setup mode
  auto-enrolls kashira's Secure Boot keys on first boot.
- `stubrepo/` holds empty packages for btrfs-progs, xfsprogs, dosfstools and
  libfido2, which mkosi's bundled initrd profile (`mkosi-initrd`) requires on
  arch but the kashira repo doesn't ship. Regenerate with
  `tools/make-stubrepo.sh`.
- `mkosi.skeleton/.../99-kashira-headless.preset` disables the Kerberos KDC
  units: `systemctl preset-all` enables everything with an [Install] section
  because kashira has no default `disable *` preset yet, and krb5-kdc /
  krb5-kadmind fail without realm config.
- `mkosi.postinst.chroot` empties the root password, adds serial-getty
  autologin on ttyS0, and flips the image's /etc/pacman.conf from the
  placeholder https repo to the local `file:///var/cache/pacman/kashira`
  build repo.

## Boot test (qemu + Secure Boot)

`tools/boot-test.py` boots the image with OVMF (secboot firmware, KVM) using
the VARS file at `/tmp/kashira-secboot-vars.fd` (keys already enrolled), mounts
the host's repo into the guest over 9p, and probes the root shell. Fresh
enrollment from scratch: delete the VARS file, copy
`/usr/share/edk2/x64/OVMF_VARS.4m.fd` in its place, boot once (sd-boot
auto-enrolls PK/KEK/db from the ESP), then boot again.

Expected: `Secure Boot: enabled (user)` in `bootctl status`,
`systemctl is-system-running` -> `running`, `pacman -Sy` syncs the kashira
repo inside the guest.

Verified 2026-09-22: kernel 7.2.7-1, systemd 261.2-4, boots to a root shell
on ttyS0 with Secure Boot enforcing.

## Known gaps / follow-ups

- UKI is ~261MB because the default kernel-modules initrd is generous; ESP is
  512MB. Fine for now, slim later (KernelModulesInitrdInclude=).
- kashira should ship a default preset policy (e.g. `disable *` + targeted
  enables in kashira-filesystem) instead of relying on per-image presets.
- Rootfs is ext4; erofs/squashfs + dm-verity is a later profile concern.
- The image's pacman.conf uses `file:///var/cache/pacman/kashira`; on real
  systems this should become the real repo URL once repo.kashiraproject.org
  exists.
