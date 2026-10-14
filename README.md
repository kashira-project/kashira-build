# kashira-build

This repository contains the host build drivers and the container build
primitive. Use `kbuild-run` for dependency-aware runs; keep the older `kbuild`
commands for direct or ordered work.

## Recommended runner

```sh
./kbuild-run run --no-publish --file <list>
./kbuild-run run --no-publish <pkgbase>...
```

`--file` takes one package base, package directory, or directory basename per
line. Positional targets use the dirty reverse-closure path and force rebuilds;
use an explicit list when the candidate set must be reviewable. `--all` is also
available, but is intentionally broad.

Use `--no-publish` for local runs. Omitting it attempts test publication, but
`kbuild-publish test` and `promote` deny publication unless
`KBUILD_ALLOW_PUBLISH=1` is set with explicit owner authorization. Inspect the
run directory and `candidates.manifest` before authorizing publication.
Archives must declare `pkgbase`; `kashira-bootstrap-*` bases cannot be published.
Test publication requires the local database objects to match the remote baseline.
Empty staging cannot replace an existing remote repository. Initializing an empty
remote requires separate `KBUILD_ALLOW_NEW_REPO=1` authorization.
A run ends as `prepared` or `published`; any build, coverage, or publication
failure is `failed`. The manifest and per-package statuses live under
`logs/runs/<KBUILD_RUN_ID>/`, with artifacts staged in that run's `out/`
directory and verified canonical copies under `out/<pkgbase>/`.

Set `KBUILD_RUN_ID` to a unique value when a run must be easy to identify or
resume around. Only one build run at a time is allowed by the host locks.

## Direct driver

```sh
./kbuild build <pkgdir>...   # independent clean-container builds
./kbuild wave <listfile>     # ordered builds, adding each result locally
```

The direct driver is useful for focused work, but `wave` has publication-like
side effects in the local repository. Do not use it as a substitute for a
reviewed `kbuild-run --no-publish` candidate.

Every build runs in a fresh container. The container sees only the local
kashira repository through `conf/pacman-kbuild.conf`; missing dependencies are
errors. The normal builder performs `-Syu`. The default public base-devel image
still contains the older GCC runtime and sysvcompat releases; its normal upgrade
path corrects them. A fresh package-owned image is verified locally as
`kashira:base-devel-takeover-20261003`; use `KBUILD_IMAGE` to select it. This local
tag has not been published and is not a portable CI default.

## Isolated Arch bootstrap (build-only)

For the explicit GNOME bootstrap, use only a copy under `/mnt/lfs/tmp/`, e.g.
`/mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/repo`. Never point the helper at
`/mnt/lfs/var/cache/pacman/kashira` or an alias. Start from a native-only copy
of `kashira.db.tar.gz` and its package archives; then:

```sh
./kbuild-arch-bootstrap init /mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/repo
./kbuild-arch-bootstrap import /mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/repo \
  /path/to/verified-archive.pkg.tar.zst https://official-arch-mirror/path/pkg.pkg.tar.zst \
  <archive-sha256> <40-hex-reviewed-signer-fingerprint> /path/to/public-only-verifier-keyring.gpg
KBUILD_REPO=/mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/repo \
KBUILD_OUT=/mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/out \
KBUILD_LOGS=/mnt/lfs/tmp/kashira-gnome-bootstrap-20261004/logs \
  ./kbuild-run run --no-publish --file <list>
```

The archive must have a detached `.sig` beside it. A separate operator must
obtain official Arch archives, establish a disposable **public-only** keyring
using official Arch master fingerprint checks, and pin the reviewed exact
signer fingerprint. The helper cryptographically verifies the signature
against the supplied keyring and fingerprint; it does not establish Arch
provenance from those inputs alone. It never fetches packages, modifies host
keyrings, or signs. Only explicitly selected imports are permitted. Missing
dependency, version/SONAME constraint, existing package name, overlapping
library (`.so` or `lib:`) capability, conflict, replace, protected core or
archive path collision rejects import. Pacman permits multiple packages to
provide the same non-core, non-library virtual capability when their names and
owned files differ; the existing native metapackage remains in the repository.
No `-Sdd` or alternate repo is used. Imports and replacement outputs share
the single isolated `kashira.db`. Import staging and commit hold the existing
`.kbuild.lock`; an incomplete commit leaves a pending marker that blocks builds
until the repository is manually recovered. The ledger registers the repository
at `init`, so a removed or corrupt mixed marker also blocks builds.

A persistent append-only ledger at `logs/arch-bootstrap-ledger.tsv` records
archive URL, SHA256, name, version and signer; its digest enters run
fingerprints. Run directories retain installed versions and exact repository
name/version/origin snapshots. Native replacement of a borrowed name requires
complete normal output validation; the ledger marks retirement without erasing
borrowed hashes. Every build from a marked repository remains assisted and
build-only even after all borrowed packages have been retired. Use fresh
native-only builds to produce publishable packages. The publisher rejects
borrowed and assisted hashes in direct, manifest, and promotion paths even
when the same bytes have been copied elsewhere. Mixed runs require
`--no-publish`, with outputs and logs below `/mnt/lfs/tmp/`. Normal native
builds keep their existing defaults.

## Reporting

```sh
./kbuild-missing <target>...
./kbuild-missing --frontier <target>...
./kbuild-missing --unvendored <target>...
```

Reports include missing outputs, unknown dependencies, unvendored bases, and
blocked edges. Unknown targets or dependency data errors return nonzero. The
frontier output is useful for triage, but its planning semantics remain under
review.

## Resources and paths

Runner defaults are `KBUILD_MEMORY=12g`, `KBUILD_MEMORY_SWAP=16g`,
`KBUILD_CPUS=8`, `KBUILD_JOBS=8`, and `KBUILD_WORKERS=2`. The two-worker host
override has been exercised at `10g` memory, `12g` swap, `6` CPUs, and `6`
jobs. `KBUILD_OVERWRITE` is empty by default; opt in only when explicitly
needed and note that it is forwarded into the container.

Paths can be overridden with `KBUILD_IMAGE`, `KBUILD_REPO`, `KBUILD_SOURCES`,
`KBUILD_OUT`, `KBUILD_LOGS`, and `KBUILD_RUN_ID`. Recent takeover evidence is
recorded under `logs/runs/takeover-roots-20261003-r1/`; media retry reached
`prepared`, while the original media run withheld publication after failure.
