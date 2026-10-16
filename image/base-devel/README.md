# Local base-devel Docker image

Builds a fresh, package-owned `base` + `base-devel` rootfs and imports it as a
local Docker image. Run as root:

```sh
sudo /mnt/kashira/src/kashira-build/image/base-devel/build.sh
# or an explicit, not-yet-existing workspace inside /mnt/kashira:
sudo /mnt/kashira/src/kashira-build/image/base-devel/build.sh /mnt/kashira/scratch/kbi-mybuild
```

`IMAGE_TAG` overrides the tag (default `kashira:base-devel-<YYYYMMDD>`),
`KIMG_REPO` the source repo (default `/mnt/kashira/repos/canonical`) and
`KIMG_OUT` the workspace parent (default `/mnt/kashira/scratch/kbimg`). The
workspace holds the rootfs, bootstrap config, install log and tmpfiles log for
inspection; the script refuses to reuse one. Nothing outside the fresh workspace
is deleted, and nothing is pushed anywhere.

## Why the workspace path must be short

gpg-agent creates its socket plus per-role auxiliary sockets inside the target
keyring (`root/etc/pacman.d/gnupg`). All of those must fit in `sun_path` (108
bytes). A long workspace path pushes them over the limit: gpg-agent exits 2,
`pacstrap -K` cannot generate a master key, and `kashira-keyring` then fails with
"There is no secret key available to sign with" while pacman still exits 0.
The script therefore defaults to `/mnt/kashira/scratch/kbimg` and rejects any workspace
whose keyring path would exceed the limit. Keep custom workspaces short.

The target root itself must also be root-owned (the script creates it as root).
`systemd-tmpfiles --root` refuses every path when the root is foreign-owned,
which produces the same silent-hook-failure class.

## What it guarantees

- `pacstrap` with `base base-devel` only, normal dependency resolution, no `-D`
  bypass, from `$KIMG_REPO` only.
- Fails on any pacman/scriptlet/hook error even when pacman exits 0.
- Checks package-owned directory ownership, non-empty CA bundle, packaged
  `pacman.conf`, keyring presence, and an offline `systemd-tmpfiles --create`.
- Leaves `/etc/machine-id` empty (fresh image identity) and removes pacman's
  package cache from the target.
- Compiles, links and executes one C and one C++17 program with the target's
  `clang` and `clang++`, verifies no GCC compiler was installed, then removes the
  temporary binaries. This catches compiler, loader and runtime mismatches that
  a package install alone can miss.

## Provenance of the current build image

`kashira:base-devel-takeover-20261003` was built by this script on 2026-10-03
(22:33 IST) from the canonical repository, then at the takeover baseline. The
baseline smoke logs are in
`/mnt/kashira/archive/kashira-build-logs-2026-10.tar.zst`
(`takeover-20261003-baseline/`). To reproduce it, rebuild from the same
repository state.

## Limits

The bootstrap repository is unsigned (`SigLevel = Never`) because the local
cache is unsigned; use a trusted local cache. The image keeps the
package-shipped `/etc/pacman.conf` for real public-repo signature checks. Docker
supplies `/etc/resolv.conf` at runtime. Runs are reproducible only while the
local repository is held fixed.

The compiler smoke test does not require `gcc`; `gcc-runtime` carries the
libstdc++ headers and GCC CRT objects that clang needs to detect the GCC
installation.
