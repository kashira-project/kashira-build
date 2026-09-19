# kashira-build — build service

P0 of `planning/build-system.md` (in the kashira repo): the container build
primitive. Later phases add the scheduler, CI wiring, and server migration.

## Usage

```sh
./kbuild build <pkgdir>...     # build pkgbases in clean containers
./kbuild wave <listfile>       # ordered wave; each result is repo-added
                               # into the local repo so later entries see it
```

- Every build runs in a fresh container from `dakkshesh07/kashira:base-devel-*`.
- The container's pacman sees ONLY the local kashira repo
  (`conf/pacman-kbuild.conf`). A missing dep fails loudly — fix by importing
  (`kashira-arch: tools/sync.py import <pkg>`), never by hand-installing.
- Artifacts: `out/<pkgbase>/`; logs + done-markers: `logs/` (waves are
  resumable).
- Sources cache: `/mnt/lfs/sources` (shared, persistent).

Env overrides: KBUILD_IMAGE, KBUILD_REPO, KBUILD_SOURCES, KBUILD_OUT,
KBUILD_LOGS.
