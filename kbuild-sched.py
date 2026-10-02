#!/usr/bin/env python3
"""kbuild-sched — dependency graph + build queue for kashira.

Parses PKGBUILDs (via makepkg --printsrcinfo) across the heart repo
(kashira-pkgs) and the fork repo (kashira-arch/pkgs), builds the pkgbase
DAG through provides, and emits a topological build plan.

Usage:
  kbuild-sched.py plan <pkgdir>...            topo-order the given pkgbases
  kbuild-sched.py wave <outfile> <pkgdir>...  write a kbuild wave file
  kbuild-sched.py dirty <pkgbase>...          reverse-dep closure of changed bases
  kbuild-sched.py graph                       debug: dump the DAG

Cycle handling: pkgbase cycles (glibc<->gcc style bootstrap) are broken by
dropping the back-edge; the broken pairs are reported on stderr.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HEART = os.environ.get("KBUILD_HEART", "/mnt/lfs/src/kashira/pkgs")
FORKS = os.environ.get("KBUILD_FORKS", "/mnt/lfs/src/kashira-arch/pkgs")


_SRCINFO_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".srcinfo-cache.json")


class SchedulerError(RuntimeError):
    pass


_METADATA_FIELDS = {"depends", "makedepends", "provides"}
_EXPRESSION_FIELDS = {"depends_exprs", "makedepends_exprs", "provides_exprs"}
_FIELD_RE = re.compile(r"^(pkgbase|pkgname|(?:depends|makedepends|provides)(?:_x86_64)?)\s*=\s*(.*)$")
_VERSION_RE = re.compile(r"(?:<=|>=|=|<|>).*", re.DOTALL)


def _metadata_name(value, field):
    value = value.strip()
    if value.endswith((":any", ":x86_64")):
        value = value.rsplit(":", 1)[0]
    if field == "provides":
        return value
    return _VERSION_RE.sub("", value).strip()


def _srcinfo_parse(pkgdir):
    """Parse one pkgdir -> dict(pkgbase, pkgnames, depends, makedepends, provides)."""
    pkgdir = os.path.realpath(pkgdir)
    try:
        with tempfile.TemporaryDirectory(prefix="kbuild-srcinfo-") as scratch:
            env = os.environ.copy()
            env.update({key: scratch for key in ("BUILDDIR", "PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST")})
            result = subprocess.run(["makepkg", "--printsrcinfo"], cwd=pkgdir, env=env,
                                    capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise SchedulerError(f"{pkgdir}: makepkg not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "command failed").strip()
        raise SchedulerError(f"{pkgdir}: makepkg --printsrcinfo failed: {detail}") from exc
    d = {"pkgbase": os.path.basename(pkgdir), "pkgdir": os.path.abspath(pkgdir),
         "pkgnames": set(), "depends": set(), "makedepends": set(), "provides": set(),
         "depends_exprs": set(), "makedepends_exprs": set(), "provides_exprs": set()}
    for raw_line in result.stdout.splitlines():
        match = _FIELD_RE.match(raw_line.strip())
        if not match:
            continue
        field, value = match.groups()
        field = field.removesuffix("_x86_64")
        if field == "pkgbase":
            d["pkgbase"] = value.strip()
        elif field == "pkgname":
            d["pkgnames"].add(value.strip())
        elif field in _METADATA_FIELDS:
            d[field + "_exprs"].add(value.strip())
            name = _metadata_name(value, field)
            if name:
                d[field].add(name)
    if not d["pkgbase"] or not d["pkgnames"]:
        raise SchedulerError(f"{pkgdir}: makepkg --printsrcinfo returned no pkgbase/pkgname")
    return d


def _recipe_stamp(pkgdir):
    files = []
    try:
        for root, dirs, names in os.walk(pkgdir, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "src", "pkg"}
                             and not os.path.islink(os.path.join(root, d)))
            for name in sorted(names):
                path = os.path.join(root, name)
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
                with open(path, "rb") as stream:
                    digest = hashlib.sha256(stream.read()).hexdigest()
                files.append({"path": os.path.relpath(path, pkgdir), "mtime_ns": stat.st_mtime_ns,
                              "size": stat.st_size, "sha256": digest,
                              "link": os.readlink(path) if os.path.islink(path) else None})
    except OSError as exc:
        raise SchedulerError(f"cannot read recipe inputs in {pkgdir}: {exc}") from exc
    if not any(item["path"] == "PKGBUILD" for item in files):
        raise SchedulerError(f"{pkgdir}: missing PKGBUILD")
    return files


def _load_cache():
    try:
        with open(_SRCINFO_CACHE, encoding="utf-8") as stream:
            cache = json.load(stream)
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _save_cache(cache):
    directory = os.path.dirname(_SRCINFO_CACHE)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".srcinfo-cache.", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(cache, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, _SRCINFO_CACHE)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass


_SRCINFO_CACHE_DATA = None


def srcinfo(pkgdir):
    """Parse cached srcinfo, invalidating entries when any local recipe input changes."""
    global _SRCINFO_CACHE_DATA
    pkgdir = os.path.realpath(pkgdir)
    if _SRCINFO_CACHE_DATA is None:
        _SRCINFO_CACHE_DATA = _load_cache()
    stamp = _recipe_stamp(pkgdir)
    ent = _SRCINFO_CACHE_DATA.get(pkgdir)
    if isinstance(ent, dict) and ent.get("version") == 4 and ent.get("stamp") == stamp:
        data = ent.get("data")
        required = ("pkgbase", "pkgdir", "pkgnames", "depends", "makedepends", "provides",
                    "depends_exprs", "makedepends_exprs", "provides_exprs")
        if (isinstance(data, dict) and all(isinstance(data.get(k), list) for k in required[2:])
                and all(isinstance(data.get(k), str) for k in required[:2])):
            return {k: (set(v) if k in _METADATA_FIELDS | _EXPRESSION_FIELDS or k == "pkgnames" else v) for k, v in data.items()}
    try:
        data = _srcinfo_parse(pkgdir)
    except SchedulerError:
        _SRCINFO_CACHE_DATA.pop(pkgdir, None)
        _save_cache(_SRCINFO_CACHE_DATA)
        raise
    _SRCINFO_CACHE_DATA[pkgdir] = {
        "version": 4,
        "stamp": stamp,
        "data": {k: (sorted(v) if isinstance(v, set) else v) for k, v in data.items()},
    }
    _save_cache(_SRCINFO_CACHE_DATA)
    return data


def universe():
    """All parseable pkgbases across both repos, keyed by pkgbase name."""
    u = {}
    for root in (HEART, FORKS):
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if not os.path.isfile(os.path.join(p, "PKGBUILD")):
                continue
            info = srcinfo(p)
            # The heart repo is visited first and wins collisions.
            u.setdefault(info["pkgbase"], info)
    return u


def provider_map(u):
    """package/provide name -> pkgbase (first writer wins; heart before forks)."""
    m = {}
    for name, info in u.items():
        for capability in sorted(info["pkgnames"] | info["provides"]):
            m.setdefault(capability, name)
            unversioned = _VERSION_RE.sub("", capability).strip()
            if unversioned:
                m.setdefault(unversioned, name)
        m.setdefault(name, name)
    return m


def build_graph(u):
    """pkgbase -> set of pkgbases it build-depends on (in-universe only)."""
    prov = provider_map(u)
    g = {}
    for name, info in u.items():
        deps = set()
        for dep in info["depends"] | info["makedepends"]:
            p = prov.get(dep)
            if p and p != name:
                deps.add(p)
        g[name] = deps
    return g


def _resolve_targets(u, args, default_all=False):
    """Resolve names, split package names, basenames, and canonical paths."""
    aliases = {}
    for name, info in u.items():
        pkgdir = os.path.realpath(info["pkgdir"])
        candidates = (name, os.path.basename(pkgdir), pkgdir,
                      os.path.abspath(info["pkgdir"]))
        candidates += tuple(sorted(info["pkgnames"]))
        for candidate in candidates:
            aliases.setdefault(candidate, name)
    if not args:
        return set(u) if default_all else set()
    wanted = set()
    unknown = []
    for arg in args:
        name = aliases.get(arg) or aliases.get(os.path.realpath(arg))
        if name is None:
            unknown.append(arg)
        else:
            wanted.add(name)
    if unknown:
        known = ", ".join(sorted(u)[:8])
        hint = f" known pkgbases include: {known}" if known else ""
        raise SchedulerError(f"unknown target(s): {', '.join(unknown)}.{hint}")
    return wanted


def _topology(g, subset=None):
    """Return deps-first order, levels, and explicitly broken cycle edges."""
    nodes = set(g) if subset is None else set(subset)
    remaining = {n: set(g.get(n, ())) & nodes for n in nodes}
    ordered = []
    levels = []
    broken = []
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            visiting = set()
            visited = set()

            def cycle_edge(node):
                visiting.add(node)
                for dep in sorted(remaining[node]):
                    if dep in visiting:
                        return node, dep
                    if dep not in visited:
                        edge = cycle_edge(dep)
                        if edge:
                            return edge
                visiting.remove(node)
                visited.add(node)
                return None

            edge = next((edge for n in sorted(remaining) if n not in visited
                         for edge in [cycle_edge(n)] if edge), None)
            if edge is None:
                raise SchedulerError("no ready node or cyclic edge in dependency graph")
            remaining[edge[0]].remove(edge[1])
            broken.append(edge)
            continue
        levels.append(ready)
        ordered.extend(ready)
        for name in ready:
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered, levels, broken


def topo_order(g, subset=None):
    """Topological order (deps first), breaking cycles. Returns (ordered, broken_edges)."""
    ordered, _levels, broken = _topology(g, subset)
    return ordered, broken


def reverse_closure(g, changed):
    """changed + every pkgbase that (transitively) build-depends on them."""
    rev = {}
    for name, deps in g.items():
        for d in deps:
            rev.setdefault(d, set()).add(name)
    seen = set(changed)
    frontier = list(changed)
    while frontier:
        cur = frontier.pop()
        for dependent in rev.get(cur, ()):
            if dependent not in seen:
                seen.add(dependent)
                frontier.append(dependent)
    return seen


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    try:
        u = universe()
        g = build_graph(u)

        if cmd == "graph":
            for name in sorted(g):
                print(f"{name}: {' '.join(sorted(g[name]))}")
            return 0

        if cmd == "plan":
            wanted = _resolve_targets(u, sys.argv[2:])
            order, broken = topo_order(g, wanted)
            for b in broken:
                print(f"cycle-break: {b[0]} -> {b[1]}", file=sys.stderr)
            for name in order:
                print(u[name]["pkgdir"])
            return 0

        if cmd == "wave":
            if len(sys.argv) < 3:
                print("wave: missing output file", file=sys.stderr)
                return 2
            outfile = sys.argv[2]
            wanted = _resolve_targets(u, sys.argv[3:])
            order, broken = topo_order(g, wanted)
            for b in broken:
                print(f"cycle-break: {b[0]} -> {b[1]}", file=sys.stderr)
            with open(outfile, "w", encoding="utf-8") as stream:
                stream.write("# generated by kbuild-sched\n")
                for name in order:
                    stream.write(u[name]["pkgdir"] + "\n")
            print(f"wave: {len(order)} pkgbases -> {outfile} (cycles broken: {len(broken)})")
            return 0

        if cmd == "dirty":
            changed = _resolve_targets(u, sys.argv[2:])
            for name in sorted(reverse_closure(g, changed)):
                print(f"{name}  {u[name]['pkgdir']}")
            return 0

        if cmd == "path":
            for name in sorted(u):
                print(f"{name}\t{u[name]['pkgdir']}")
            return 0

        if cmd == "levels":
            wanted = _resolve_targets(u, sys.argv[2:], default_all=True)
            _order, levels, broken = _topology(g, wanted)
            for b in broken:
                print(f"cycle-break: {b[0]} -> {b[1]}", file=sys.stderr)
            for members in levels:
                print(" ".join(members))
            return 0

        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    except (OSError, SchedulerError) as exc:
        print(f"kbuild-sched: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
