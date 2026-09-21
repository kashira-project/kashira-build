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
import os
import subprocess
import sys
from graphlib import TopologicalSorter

HEART = os.environ.get("KBUILD_HEART", "/mnt/lfs/src/kashira/pkgs")
FORKS = os.environ.get("KBUILD_FORKS", "/mnt/lfs/src/kashira-arch/pkgs")


_SRCINFO_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".srcinfo-cache.json")


def _srcinfo_parse(pkgdir):
    """Parse one pkgdir -> dict(pkgbase, pkgnames, depends, makedepends, provides)."""
    try:
        out = subprocess.run(["makepkg", "--printsrcinfo"], cwd=pkgdir,
                             capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return None
    d = {"pkgbase": os.path.basename(pkgdir), "pkgdir": pkgdir,
         "pkgnames": set(), "depends": set(), "makedepends": set(), "provides": set()}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pkgbase = "):
            d["pkgbase"] = line.split(" = ", 1)[1]
        elif line.startswith("pkgname = "):
            d["pkgnames"].add(line.split(" = ", 1)[1])
        for field in ("depends", "makedepends", "provides"):
            if line.startswith(field + " = ") or line.startswith(field + "_x86_64 = "):
                # strip version constraints and arch conditions
                v = line.split(" = ", 1)[1].split("<")[0].split(">")[0].split("=")[0].strip()
                d[field].add(v)
    return d


def srcinfo(pkgdir):
    """Cached srcinfo parse; invalidates on PKGBUILD mtime change."""
    import json
    cache = {}
    if os.path.exists(_SRCINFO_CACHE):
        try:
            cache = json.load(open(_SRCINFO_CACHE))
        except Exception:
            cache = {}
    stamp = os.path.getmtime(os.path.join(pkgdir, "PKGBUILD"))
    ent = cache.get(pkgdir)
    if ent and ent[0] == stamp:
        return {k: (set(v) if isinstance(v, list) else v) for k, v in ent[1].items()}
    d = _srcinfo_parse(pkgdir)
    if d:
        cache[pkgdir] = [stamp, {k: (sorted(v) if isinstance(v, set) else v)
                                 for k, v in d.items()}]
        tmp = _SRCINFO_CACHE + ".tmp"
        json.dump(cache, open(tmp, "w"))
        os.replace(tmp, _SRCINFO_CACHE)
    return d


def universe():
    """All pkgbases across both repos, keyed by pkgbase name."""
    u = {}
    for root in (HEART, FORKS):
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if os.path.isfile(os.path.join(p, "PKGBUILD")):
                info = srcinfo(p)
                if info:
                    u[info["pkgbase"]] = info
    return u


def provider_map(u):
    """package/provide name -> pkgbase (first writer wins; heart before forks)."""
    m = {}
    for name, info in u.items():
        for n in info["pkgnames"] | info["provides"]:
            m.setdefault(n, name)
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


def topo_order(g, subset=None):
    """Topological order (deps first), breaking cycles. Returns (ordered, broken_edges)."""
    broken = []
    ts = TopologicalSorter(g)
    try:
        ordered = list(ts.static_order())
    except Exception:
        # cycle: drop one edge per cycle and retry
        ordered = []
        remaining = dict(g)
        while remaining:
            ts = TopologicalSorter(remaining)
            try:
                ordered.extend(ts.static_order())
                break
            except Exception:
                # find a cycle and drop its first edge
                done = ts.get_ready() if hasattr(ts, "get_ready") else None
                # graphlib raises CycleError with the cycle as arg
                try:
                    ts.prepare()
                except Exception as ce:
                    cyc = ce.args[1] if len(ce.args) > 1 else None
                    if cyc and len(cyc) >= 2:
                        a, b = cyc[0], cyc[1]
                        if b in remaining.get(a, set()):
                            remaining[a] = remaining[a] - {b}
                            broken.append((a, b))
                        else:
                            remaining[a] = remaining.get(a, set()) - {cyc[1]} if len(cyc) > 1 else remaining.get(a, set())
                    else:
                        # can't recover; emit rest unordered
                        ordered.extend(sorted(remaining))
                        break
    if subset:
        order = [n for n in ordered if n in subset]
        return order, broken
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
    u = universe()
    g = build_graph(u)

    if cmd == "graph":
        for name in sorted(g):
            print(f"{name}: {' '.join(sorted(g[name]))}")
        return 0

    if cmd == "plan":
        # accept pkgbase names, directory basenames, or pkgdir paths
        bypath = {}
        for name, info in u.items():
            bypath[info["pkgdir"]] = name
            bypath[os.path.basename(info["pkgdir"])] = name
            bypath[name] = name
        wanted = {bypath.get(a, a) for a in sys.argv[2:]}
        order, broken = topo_order(g, wanted)
        for b in broken:
            print(f"cycle-break: {b[0]} -> {b[1]}", file=sys.stderr)
        for name in order:
            print(u[name]["pkgdir"])
        return 0

    if cmd == "wave":
        outfile = sys.argv[2]
        wanted = set(sys.argv[3:])
        order, broken = topo_order(g, wanted)
        with open(outfile, "w") as f:
            f.write("# generated by kbuild-sched\n")
            for name in order:
                f.write(u[name]["pkgdir"] + "\n")
        print(f"wave: {len(order)} pkgbases -> {outfile} (cycles broken: {len(broken)})")
        return 0

    if cmd == "dirty":
        changed = sys.argv[2:]
        for name in sorted(reverse_closure(g, changed)):
            print(f"{name}  {u[name]['pkgdir']}")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
