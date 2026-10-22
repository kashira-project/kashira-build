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

Use `--no-publish` for local runs. Omitting it publishes to `x86_64-v3` (debug packages to `x86_64-v3-debug`), but
`kbuild-publish publish` denies publication unless
`KBUILD_ALLOW_PUBLISH=1` is set with explicit owner authorization. Inspect the
run directory and `candidates.manifest` before authorizing publication.
Archives must declare `pkgbase`; `kashira-bootstrap-*` bases cannot be published.
Publication requires the local database objects to match the remote baseline.
Empty staging cannot replace an existing remote repository. Initializing an empty
remote requires separate `KBUILD_ALLOW_NEW_REPO=1` authorization.
A run ends as `prepared` or `published`; any build, coverage, or publication
failure is `failed`. The manifest and per-package statuses live under
`$KBUILD_LOGS/runs/<KBUILD_RUN_ID>/`, with artifacts staged in that run's `out/`
directory and verified canonical copies under `$KBUILD_OUT/<pkgbase>/`.

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

Every build runs in a fresh container. `kbuild-inside.sh` generates its
pacman.conf: the local kashira repository first, then Arch snapshot stand-ins
(`kbuild-standin`, frozen at `conf/standin.date`) for anything not built yet.
`kbuild-pool` is the parallel runner; see kashira `kb/30-build.md`. The default image is the local,
package-owned `kashira:base-devel-takeover-20261003` (built by
`image/base-devel/build.sh` on 2026-10-03); it has not been published and is not
a portable CI default.

## Isolated Arch bootstrap (build-only)

The GNOME bootstrap candidate repository is `/mnt/kashira/repos/candidate`
(the default `KBUILD_REPO`). Other mixed repositories may live only below
`/mnt/kashira/scratch/`. Never point the helper at `/mnt/kashira/repos/canonical`
or an alias. Start from a native-only copy of `kashira.db.tar.gz` and its
package archives; then:

```sh
./kbuild-arch-bootstrap init /mnt/kashira/scratch/<name>/repo
./kbuild-arch-bootstrap import /mnt/kashira/scratch/<name>/repo \
  /path/to/verified-archive.pkg.tar.zst https://official-arch-mirror/path/pkg.pkg.tar.zst \
  <archive-sha256> <40-hex-reviewed-signer-fingerprint> /path/to/public-only-verifier-keyring.gpg
KBUILD_RUN_ID=<unique-id> ./kbuild-run run --no-publish --file <list>
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

A persistent append-only ledger at `/mnt/kashira/build/logs/arch-bootstrap-ledger.tsv`
(rows keyed by the resolved repository path) records
archive URL, SHA256, name, version and signer; its digest enters run
fingerprints. Run directories retain installed versions and exact repository
name/version/origin snapshots. Native replacement of a borrowed name requires
complete normal output validation; the ledger marks retirement without erasing
borrowed hashes. Every build from a marked repository remains assisted and
build-only even after all borrowed packages have been retired. Use fresh
native-only builds to produce publishable packages. The publisher rejects
borrowed and assisted hashes in direct, manifest, and promotion paths even
when the same bytes have been copied elsewhere. Mixed runs require
`--no-publish`, with outputs and logs below `/mnt/kashira/build/` or
`/mnt/kashira/scratch/`.

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

Defaults live in `kbuild-lib.sh`: `KBUILD_MEMORY=unlimited` (no cgroup cap;
owner decision), `KBUILD_CPUS=16`, `KBUILD_JOBS=16`, and `KBUILD_WORKERS=2`. `KBUILD_OVERWRITE` is empty by default; opt in only when explicitly
needed and note that it is forwarded into the container.

Paths default to the `/mnt/kashira` workspace (`KASHIRA_ROOT`): repo
`repos/candidate`, sources `cache/sources`, output `build/out`, logs
`build/logs`. Override with `KASHIRA_ROOT`, `KBUILD_IMAGE`, `KBUILD_REPO`,
`KBUILD_SOURCES`, `KBUILD_OUT`, `KBUILD_LOGS`, and `KBUILD_RUN_ID`. Build
fingerprints include these absolute paths, so changing them resets artifact
reuse. Nothing is written inside this checkout.
