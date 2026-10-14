# kashira headless image (mkosi)

Bootable kashira disk image with systemd-boot and a signed UKI. Signing is
kept for artifacts, but the normal image never enrolls firmware keys.

## Build

Host needs `mkosi` v27, `systemd-ukify`, `sbsigntools`, and the existing local
package cache. Run the existing documented builder from this directory:

```sh
cd /mnt/lfs/src/kashira-build/image/headless
tools/make-stubrepo.sh   # audit only; must pass before building
sudo mkosi summary
sudo mkosi build          # add --force to rebuild over existing output
```

The unsigned Secure-Boot-off local acceptance build uses no production signing
key and keeps old outputs intact:

```sh
sudo -n mkosi --secure-boot=no --secure-boot-auto-enroll=no \
  --output=kashira-headless-networked \
  --output-directory=/mnt/lfs/tmp/kashira-headless-networked \
  --workspace-directory=/mnt/lfs/tmp/khi build
```

This produces a tested disk/USB image, not a validated installer ISO. Signed
builds and Secure Boot-on acceptance remain separate validation steps.

The audit must report real `btrfs-progs`, `xfsprogs`, `dosfstools`, and
`libfido2` packages in `/mnt/lfs/var/cache/pacman/kashira`. Empty stand-ins are
not supported. The build cache is unsigned bootstrap input only; the installed
`/etc/pacman.conf` remains the package-shipped signed public repository config.

The normal image locks root, has no serial autologin, configures wired DHCP
through networkd and uses systemd-resolved's DNS stub. Image builds use the
isolated `/mnt/lfs/tmp/khi-pkg-cache` instead of the host distribution's package
cache. The explicit headless preset is applied offline under `--root` and named
`90-kashira-headless.preset` so its enable rules win before the packaged distro
`99-kashira-default.preset` disable catch-all. Existing raw/EFI/kernel/initrd
files are build outputs and are not changed by configuration validation.

## Secure Boot

`SecureBoot=yes` signs systemd-boot and the UKI, while
`SecureBootAutoEnroll=no` prevents the image from enrolling keys. Acceptance
runs default to firmware Secure Boot off. `--secure-boot on` with the default
fresh, unenrolled public OVMF vars template is a negative policy test and is
expected to fail boot-policy. A positive Secure Boot test requires an
explicitly supplied already-enrolled vars template through a future option;
there is no automatic enrollment and no host firmware key access.

## Authoritative boot acceptance

The standard-library harness uses fresh temporary paths and snapshot storage:

```sh
python3 tools/boot-test.py \
  --image ./kashira-headless.raw \
  --firmware /usr/share/edk2/x64/OVMF_CODE.4m.fd \
  --workdir /mnt/lfs/tmp/kashira-boot-logs
```

The default test boots the unmodified snapshot and accepts only a real serial
login prompt. It does not send shell input, and fails if the prompt shows
automatic login or a shell. Its report is explicitly `login-boot`; it does not
claim the administrative probe suite passed. Network readiness is checked in
the disposable administrative mode: a global IPv4 address, an IPv4 default
route, and DNS resolution of `repo.kashiraproject.org` must all work.

For disposable VM debugging only, `--debug-overlay` creates a temporary copied
raw image, adds a serial root autologin drop-in, and runs the authoritative
administrative probes, including root lock, failed-unit, package, preset, and
network checks. The overlay is never part of the build output. A network
failure is a real acceptance failure and requires a profile/network fix; it is
not silently disabled:

```sh
sudo -n python3 tools/boot-test.py \
  --image /mnt/lfs/tmp/kashira-headless-takeover/kashira-headless-takeover.raw \
  --timeout 120 \
  --workdir /mnt/lfs/tmp/kashira-headless-boot-r3

sudo -n python3 tools/boot-test.py --debug-overlay \
  --image /mnt/lfs/tmp/kashira-headless-takeover/kashira-headless-takeover.raw \
  --timeout 120 \
  --workdir /mnt/lfs/tmp/kashira-headless-boot-r3
```

The first command reports only `login-boot`; the second reports
`administrative-checks`. Use the parent-supplied `--config` JSON for exact
package versions and required units. No host repository keys, signing keys, or
firmware vars are mounted read-write.
