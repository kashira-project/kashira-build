#!/usr/bin/env python3
"""kbuild-sched — dependency graph + build queue for kashira.

Parses PKGBUILDs (via makepkg --printsrcinfo) across the kashira-pkgs tree
(<first-char>/<pkgbase>/PKGBUILD), builds the pkgbase DAG through provides, and emits a topological build plan.

Usage:
  kbuild-sched.py plan <pkgdir>...            topo-order the given pkgbases
  kbuild-sched.py wave <outfile> <pkgdir>...  write a kbuild wave file
  kbuild-sched.py dirty <pkgbase>...          reverse-dep closure of changed bases
  kbuild-sched.py graph                       debug: dump the DAG

  kbuild-sched.py levels <pkgbase>...         build levels (cycles resolved as below)
  kbuild-sched.py stage1 <pkgbase>...         dependencies stage-1 builds go without

Cycle handling: runtime cycles through KBUILD_BOOTSTRAP_EDGES policy; a makedepends the build repo (KBUILD_REPO)
already satisfies does not order the run (the repo copy is installed, as Arch does); a cycle left among unbuilt
targets gets one stage-1 member built without its in-cycle partners and rebuilt once they exist.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.environ.get("KASHIRA_ROOT", "/mnt/kashira")
PKGS = os.environ.get("KBUILD_PKGS", f"{ROOT}/src/kashira-pkgs")


_SRCINFO_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".srcinfo-cache.json")


class SchedulerError(RuntimeError):
    pass


_METADATA_FIELDS = {"depends", "makedepends", "provides"}
_EXPRESSION_FIELDS = {"depends_exprs", "makedepends_exprs", "provides_exprs"}
_FIELD_RE = re.compile(r"^(pkgbase|pkgname|pkgver|pkgrel|epoch|(?:depends|makedepends|provides)(?:_x86_64)?)\s*=\s*(.*)$")
_VERSION_RE = re.compile(r"(?:<=|>=|=|<|>).*", re.DOTALL)
_APPROVED_BOOTSTRAP_EDGES = {
    ("gtk4", "adwaita-icon-theme"),
    ("gdm", "gnome-shell"),
    ("pipewire", "wireplumber"),
    # Runtime-only edge whose reverse edge is a build dependency (2026-10-14):
    # the portal service is not needed to build flatpak.
    ("flatpak", "xdg-desktop-portal"),
    # gnome-shell build-depends on gnome-control-center (keybinding XML);
    # control-center only needs the shell at runtime.
    ("gnome-control-center", "gnome-shell"),
    # sysprof's GTK UI needs gtk4 at runtime only; gtk4 build-depends sysprof (via gstreamer/libsoup3),
    # so sysprof is built first against the previous gtk4 already in the candidate repo (2026-10-20).
    ("sysprof", "gtk4"),
    ("sysprof", "libadwaita"),
}


def _metadata_name(value, field):
    value = value.strip()
    if value.endswith((":any", ":x86_64")):
        value = value.rsplit(":", 1)[0]
    if field == "provides":
        return value
    return _VERSION_RE.sub("", value).strip()


def _split_expr(expr):
    expr = expr.strip()
    match = re.match(r"^(.+?)(<=|>=|=|<|>)(.+)$", expr)
    if match:
        name, op, version = match.groups()
    else:
        name, op, version = expr, None, None
    if name.endswith((":any", ":x86_64")):
        name = name.rsplit(":", 1)[0]
    return name, op, version.strip() if version else None


def _version_matches(version, op, wanted):
    if op is None:
        return True
    if version is None:
        return False
    try:
        result = subprocess.run(["vercmp", version, wanted], capture_output=True, text=True, check=True)
        comparison = int(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise SchedulerError(f"vercmp unavailable or failed for {version!r}, {wanted!r}: {exc}") from exc
    return {"=": comparison == 0, ">": comparison > 0, ">=": comparison >= 0,
            "<": comparison < 0, "<=": comparison <= 0}[op]


def _srcinfo_parse(pkgdir):
    """Parse one pkgdir, retaining effective dependency metadata per output."""
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
         "depends_exprs": set(), "makedepends_exprs": set(), "provides_exprs": set(),
         "outputs": {}, "pkgver": "", "pkgrel": "", "epoch": "0"}
    global_fields = {field: [] for field in _METADATA_FIELDS}
    global_arch_fields = {field: [] for field in _METADATA_FIELDS}
    raw_outputs = {}
    current = None
    for raw_line in result.stdout.splitlines():
        match = _FIELD_RE.match(raw_line.strip())
        if not match:
            continue
        raw_field, value = match.groups()
        arch_specific = raw_field.endswith("_x86_64")
        field = raw_field.removesuffix("_x86_64")
        value = value.strip()
        if field == "pkgbase":
            d["pkgbase"] = value
            current = None
        elif field == "pkgname":
            current = value
            d["pkgnames"].add(value)
            raw_outputs.setdefault(value, {})
        elif field in ("pkgver", "pkgrel", "epoch") and current is None:
            d[field] = value
        elif field in _METADATA_FIELDS:
            if value:
                d[field + "_exprs"].add(value)
                name = _metadata_name(value, field)
                if name:
                    d[field].add(name)
            if current is None:
                (global_arch_fields if arch_specific else global_fields)[field].append(value)
            else:
                raw_outputs[current].setdefault(field + ("_arch" if arch_specific else ""), []).append(value)
    if not d["pkgbase"] or not d["pkgnames"]:
        raise SchedulerError(f"{pkgdir}: makepkg --printsrcinfo returned no pkgbase/pkgname")
    version = d["pkgver"]
    if d["pkgrel"]:
        version += "-" + d["pkgrel"]
    if d["epoch"] != "0":
        version = d["epoch"] + ":" + version
    for pkgname, values in raw_outputs.items():
        output = {"version": version}
        for field in _METADATA_FIELDS:
            generic = values[field] if field in values else global_fields[field]
            arch = values[field + "_arch"] if field + "_arch" in values else global_arch_fields[field]
            output[field + "_exprs"] = sorted(set(value for value in generic + arch if value))
        d["outputs"][pkgname] = output
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
                # content, not mtime: materialize rewrites identical PKGBUILDs
                files.append({"path": os.path.relpath(path, pkgdir),
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


def srcinfo(pkgdir, save=True):
    """Parse cached srcinfo, invalidating entries when any local recipe input changes."""
    global _SRCINFO_CACHE_DATA
    pkgdir = os.path.realpath(pkgdir)
    if _SRCINFO_CACHE_DATA is None:
        _SRCINFO_CACHE_DATA = _load_cache()
    stamp = _recipe_stamp(pkgdir)
    ent = _SRCINFO_CACHE_DATA.get(pkgdir)
    if isinstance(ent, dict) and ent.get("version") == 8 and ent.get("stamp") == stamp:
        data = ent.get("data")
        required = ("pkgbase", "pkgdir", "pkgnames", "depends", "makedepends", "provides",
                    "depends_exprs", "makedepends_exprs", "provides_exprs", "outputs",
                    "pkgver", "pkgrel", "epoch")
        if (isinstance(data, dict) and all(isinstance(data.get(k), list) for k in required[2:10] if k != "outputs")
                and isinstance(data.get("outputs"), dict)
                and all(isinstance(data.get(k), str) for k in required[:2] + required[10:])):
            return {k: (set(v) if k in _METADATA_FIELDS | _EXPRESSION_FIELDS or k == "pkgnames" else v) for k, v in data.items()}
    try:
        data = _srcinfo_parse(pkgdir)
    except SchedulerError:
        _SRCINFO_CACHE_DATA.pop(pkgdir, None)
        if save:
            _save_cache(_SRCINFO_CACHE_DATA)
        raise
    _SRCINFO_CACHE_DATA[pkgdir] = {
        "version": 8,
        "stamp": stamp,
        "data": {k: (sorted(v) if isinstance(v, set) else v) for k, v in data.items()},
    }
    if save:
        _save_cache(_SRCINFO_CACHE_DATA)
    return data


def universe():
    """All parseable pkgbases in the tree, keyed by pkgbase name."""
    u = {}
    if not os.path.isdir(PKGS):
        return u
    dirs = [os.path.join(PKGS, shard, d) for shard in sorted(os.listdir(PKGS))
            if len(shard) == 1 and os.path.isdir(os.path.join(PKGS, shard))
            for d in sorted(os.listdir(os.path.join(PKGS, shard)))
            if os.path.isfile(os.path.join(PKGS, shard, d, "PKGBUILD"))]
    # parse cache misses in parallel and write the cache once (per-entry saves are quadratic at 3000+ recipes)
    from concurrent.futures import ThreadPoolExecutor
    global _SRCINFO_CACHE_DATA
    if _SRCINFO_CACHE_DATA is None:
        _SRCINFO_CACHE_DATA = _load_cache()
    infos = {}
    with ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        for p, fut in [(p, ex.submit(srcinfo, p, False)) for p in dirs]:
            try:
                infos[p] = fut.result()
            except SchedulerError as exc:
                infos[p] = exc
    _save_cache(_SRCINFO_CACHE_DATA)
    for p in dirs:
        info = infos[p]
        if isinstance(info, SchedulerError):
            raise info
        if info["pkgbase"] in u:
            raise SchedulerError(f"duplicate pkgbase {info['pkgbase']}: {p} and {u[info['pkgbase']]['pkgdir']}")
        u[info["pkgbase"]] = info
    return u


def _provider_map(u):
    providers = {}
    for base, info in u.items():
        for pkgname, output in info["outputs"].items():
            capabilities = [(pkgname, output["version"])]
            capabilities.extend((_split_expr(expr)[0], _split_expr(expr)[2])
                                for expr in output.get("provides_exprs", ()))
            for capability, version in capabilities:
                providers.setdefault(capability, []).append((base, pkgname, version))
    extra = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conf", "provides.extra")
    for line in open(extra) if os.path.isfile(extra) else ():
        fields = line.split("#")[0].split()
        if len(fields) == 2 and fields[1] in u:
            providers.setdefault(fields[0], []).append((fields[1], fields[1], ""))
    return providers


def _resolve_provider(providers, expr):
    name, op, wanted = _split_expr(expr)
    candidates = [candidate for candidate in providers.get(name, ())
                  if _version_matches(candidate[2], op, wanted)]
    if not candidates:
        return None
    return candidates[0]


def provider_map(u):
    """package/provide name -> pkgbase (first writer wins; heart before forks)."""
    return {name: candidates[0][0] for name, candidates in _provider_map(u).items()}


def build_edges(u):
    providers = _provider_map(u)
    graph = {base: set() for base in u}
    edges = {}
    for base, info in u.items():
        for output_name, output in info["outputs"].items():
            for field in ("depends_exprs", "makedepends_exprs"):
                for expr in output.get(field, ()):
                    provider = _resolve_provider(providers, expr)
                    if provider and provider[0] == base:
                        # self-hosting (ghc makedepends ghc-static): order after a seed that also provides it
                        name, op, wanted = _split_expr(expr)
                        provider = next((c for c in providers.get(name, ()) if c[0] != base
                                         and _version_matches(c[2], op, wanted)), provider)
                    if provider and provider[0] != base:
                        target = provider[0]
                        graph[base].add(target)
                        edges.setdefault((base, target), []).append({
                            "field": field.removesuffix("_exprs"),
                            "expr": expr,
                            "output": output_name,
                            "provider": provider[1],
                        })
    return graph, edges


def build_graph(u):
    """pkgbase -> set of pkgbases it build-depends on (in-universe only)."""
    return build_edges(u)[0]


def parse_policy(value=None):
    value = os.environ.get("KBUILD_BOOTSTRAP_EDGES", "") if value is None else value
    tokens = [token for token in re.split(r"[\s,]+", value.strip()) if token]
    result = []
    seen = set()
    for token in tokens:
        match = re.fullmatch(r"([A-Za-z0-9@._+:-]+)>([A-Za-z0-9@._+:-]+)", token)
        if not match:
            raise SchedulerError(f"invalid KBUILD_BOOTSTRAP_EDGES entry: {token}")
        edge = match.groups()
        if edge in seen:
            raise SchedulerError(f"duplicate KBUILD_BOOTSTRAP_EDGES entry: {token}")
        seen.add(edge)
        result.append(edge)
    return sorted(result)


def _cycle_path(graph, source, target, ignored=()):
    ignored = set(ignored)
    stack = [(target, [target])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node == source:
            return path
        if node in seen:
            continue
        seen.add(node)
        for dep in sorted(graph.get(node, ())):
            if (node, dep) in ignored:
                continue
            stack.append((dep, path + [dep]))
    return None


def validate_policy(u, graph, edges, subset=None):
    selected = set(u) if subset is None else set(subset)
    policy = parse_policy()
    for source, target in policy:
        if (source, target) not in _APPROVED_BOOTSTRAP_EDGES:
            raise SchedulerError(f"bootstrap edge {source}>{target}: not approved")
        if source not in u or target not in u:
            raise SchedulerError(f"bootstrap edge {source}>{target}: unknown pkgbase")
        if source not in selected or target not in selected:
            continue                       # cannot close a cycle inside this run; irrelevant here
        records = edges.get((source, target), ())
        runtime = [record for record in records if record["field"] == "depends"]
        makedep = [record for record in records if record["field"] == "makedepends"]
        if makedep:
            if runtime:
                exprs = ", ".join(sorted({record["expr"] for record in makedep}))
                raise SchedulerError(f"bootstrap edge {source}>{target}: mixed depends/makedepends provenance; retained makedepends: {exprs}")
            raise SchedulerError(f"bootstrap edge {source}>{target}: only makedepends, not runtime depends")
        if not runtime:
            raise SchedulerError(f"bootstrap edge {source}>{target}: not a dependency edge")
        scoped_graph = {node: set(graph.get(node, ())) & selected for node in selected}
        path = _cycle_path(scoped_graph, source, target, ignored={(source, target)})
        if not path:
            raise SchedulerError(f"bootstrap edge {source}>{target}: not part of a cycle")
    return [(s, t) for s, t in policy if s in selected and t in selected]


def _cycle_edges(graph, subset):
    nodes = set(subset)
    remaining = {node: set(graph.get(node, ())) & nodes for node in nodes}
    visiting, visited, found = set(), set(), []

    def visit(node, path):
        visiting.add(node)
        for dep in sorted(remaining[node]):
            if dep in visiting:
                start = path.index(dep)
                found.extend(zip(path[start:], path[start + 1:] + [dep]))
            elif dep not in visited:
                visit(dep, path + [dep])
        visiting.remove(node)
        visited.add(node)

    for node in sorted(nodes):
        if node not in visited:
            visit(node, [node])
    return sorted(set(found))


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


def _repo_satisfied(edges, cut, repo):
    """Edges in `cut` whose every record (depends or makedepends) the build repo already satisfies: the repo copy is
    what gets installed for the build, so the edge need not order the run."""
    out = set()
    for source, target in cut:
        records = edges.get((source, target), ())
        if records and all(any(
                _version_matches(v, _split_expr(r["expr"])[1], _split_expr(r["expr"])[2])
                for n, v in repo.get(_split_expr(r["expr"])[0], ()) if n == _split_expr(r["expr"])[0])
                for r in records):
            out.add((source, target))
    return out


def _sccs(graph):
    """Strongly connected components with more than one node (iterative Tarjan)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter[0]; counter[0] += 1; stack.append(root); on.add(root)
        while work:
            node, it = work[-1]
            for dep in it:
                if dep not in index:
                    index[dep] = low[dep] = counter[0]; counter[0] += 1; stack.append(dep); on.add(dep)
                    work.append((dep, iter(sorted(graph[dep]))))
                    break
                if dep in on:
                    low[node] = min(low[node], index[dep])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        x = stack.pop(); on.discard(x); comp.append(x)
                        if x == node:
                            break
                    if len(comp) > 1:
                        out.append(sorted(comp))
    return out


def stage1_cuts(u, graph, edges, nodes, ignored, repo):
    """Break the cycles left among `nodes` once repo-satisfied makedepends are ignored.

    Returns (soft, stage1): soft edges are deps the repo already satisfies (the repo copy is installed, the edge does
    not order the run); stage1 maps a pkgbase to the in-cycle edges it is built without. A stage-1 build is
    never final: its partners are not installed, so it is rebuilt once the whole cycle exists in the repo.
    """
    live = {n: {d for d in graph.get(n, ()) if d in nodes and (n, d) not in ignored} for n in nodes}
    soft = _repo_satisfied(edges, {(n, d) for n, ds in live.items() for d in ds}, repo) if repo else set()
    for n, d in soft:
        live[n].discard(d)
    stage1 = {}
    for comp in _sccs(live):
        for n, d in _feedback_edges(comp, live, edges):
            stage1.setdefault(n, set()).add(d)
    return soft, stage1


def _feedback_edges(comp, live, edges):
    """Edges to cut so `comp` becomes acyclic: weighted Eades-Lin-Smyth ordering of the build-before graph.
    Cutting a makedepends (often docs/tooling) is cheap; cutting a runtime depends is avoided (weight 10)."""
    members = set(comp)
    weight = {(n, d): 10 if any(r["field"] == "depends" for r in edges.get((n, d), ())) else 1
              for n in comp for d in live[n] & members}
    need = {n: {d for d in live[n] & members} for n in comp}            # n needs d: d builds first
    users = {n: set() for n in comp}
    for n, ds in need.items():
        for d in ds:
            users[d].add(n)
    front, back, left = [], [], set(comp)
    while left:
        moved = True
        while moved:
            moved = False
            for n in sorted(left):
                if not need[n] & left:                                   # nothing left to wait for
                    front.append(n); left.discard(n); moved = True
                elif not users[n] & left:                                # nothing left waits for it
                    back.append(n); left.discard(n); moved = True
        if left:
            score = lambda n: (sum(weight[(u, n)] for u in users[n] & left) - sum(weight[(n, d)] for d in need[n] & left), n)
            pick = max(sorted(left), key=score)                          # most wanted, least needy: build early
            front.append(pick); left.discard(pick)
    pos = {n: i for i, n in enumerate(front + back[::-1])}
    return sorted((n, d) for n in comp for d in need[n] if pos[d] > pos[n])


def _retarget(u, graph, edges, nodes):
    """A dep whose first provider is outside the run but another provider is in it (a seed: jdk-bootstrap provides
    java-environment) orders against the one in the run."""
    providers = _provider_map(u)
    graph = {n: set(ds) for n, ds in graph.items()}
    edges = dict(edges)
    for (source, target), records in list(edges.items()):
        if source not in nodes or target in nodes:
            continue
        for r in records:
            name, op, wanted = _split_expr(r["expr"])
            alt = next((c for c in providers.get(name, ()) if c[0] in nodes and c[0] != source
                        and _version_matches(c[2], op, wanted)), None)
            if alt:
                graph[source].add(alt[0])
                edges.setdefault((source, alt[0]), []).append(dict(r, provider=alt[1]))
    return graph, edges


def _build_repo():
    """Capabilities of the build repo (KBUILD_REPO), or {} when it has no db yet."""
    path = os.path.join(os.environ.get("KBUILD_REPO", f"{ROOT}/repos/candidate"), "kashira.db.tar.gz")
    return _repo_capabilities(path) if os.path.isfile(path) else {}


def _topology(u, graph, edges, subset=None, repo=None):
    """Return deps-first order after removing validated runtime policy edges, repo-satisfied makedepends, and
    stage-1 cuts (see stage1_cuts)."""
    nodes = set(graph) if subset is None else set(subset)
    graph, edges = _retarget(u, graph, edges, nodes)
    policy = validate_policy(u, graph, edges, nodes)
    ignored = set(policy)
    if repo is not None:
        soft, stage1 = stage1_cuts(u, graph, edges, nodes, ignored, repo)
        ignored |= soft | {(n, d) for n, ds in stage1.items() for d in ds}
    cycles = _cycle_edges({node: {dep for dep in graph.get(node, ())
                                  if (node, dep) not in ignored} for node in nodes}, nodes)
    if cycles:
        typed = []
        for source, target in cycles:
            details = edges.get((source, target), ())
            kinds = ",".join(sorted({detail["field"] for detail in details})) or "unknown"
            typed.append(f"{source}>{target} ({kinds})")
        raise SchedulerError("unapproved dependency cycle: " + ", ".join(typed))
    remaining = {node: {dep for dep in graph.get(node, ()) if dep in nodes and (node, dep) not in ignored}
                 for node in nodes}
    levels = []
    while remaining:
        ready = sorted(node for node, deps in remaining.items() if not deps)
        if not ready:
            raise SchedulerError("unapproved dependency cycle")
        levels.append(ready)
        for node in ready:
            del remaining[node]
        for deps in remaining.values():
            deps.difference_update(ready)
    return [node for level in levels for node in level], levels, policy


def topo_order(u, graph, edges, subset=None):
    ordered, _levels, policy = _topology(u, graph, edges, subset)
    return ordered, policy


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


def _repo_capabilities(repo_path):
    records = {}
    if not os.path.isfile(repo_path):
        raise SchedulerError(f"missing read-only package repo: {repo_path}")
    try:
        with tarfile.open(repo_path) as archive:
            for member in archive:
                if not member.name.endswith("/desc"):
                    continue
                fields = {}
                key = None
                data = archive.extractfile(member).read().decode(errors="replace")
                for line in data.splitlines():
                    if line.startswith("%") and line.endswith("%"):
                        key = line.strip("%")
                        fields[key] = []
                    elif line and key:
                        fields[key].append(line)
                name = (fields.get("NAME") or [""])[0]
                version = (fields.get("VERSION") or [""])[0]
                arch = (fields.get("ARCH") or [""])[0]
                if name and version and arch in ("x86_64", "any", ""):
                    capabilities = [(name, version)]
                    capabilities.extend((_split_expr(expr)[0], _split_expr(expr)[2])
                                        for expr in fields.get("PROVIDES", ()))
                    records.setdefault(name, []).extend(capabilities)
                    for capability in capabilities:
                        records.setdefault(capability[0], []).append(capability)
    except (OSError, tarfile.TarError) as exc:
        raise SchedulerError(f"cannot read package repo {repo_path}: {exc}") from exc
    return records


def _standin_dbs():
    """The filtered stand-in DBs build containers see (kbuild-standin), unless KBUILD_STANDIN=0."""
    if os.environ.get("KBUILD_STANDIN", "1") == "0":
        return []
    here = os.path.dirname(os.path.realpath(__file__))
    date = open(os.path.join(here, "conf/standin.date")).read().strip().replace("/", "-")
    base = os.path.join(os.environ.get("KASHIRA_ROOT", "/mnt/kashira"), "cache/standin", date, "filtered")
    return [os.path.join(base, r + ".db") for r in ("core", "extra", "multilib")]


def preflight(u, graph, edges, targets, repo_path):
    selected = set(targets)
    policy = set(validate_policy(u, graph, edges, selected))
    repo = _repo_capabilities(repo_path)
    for db in _standin_dbs():                           # a stand-in satisfies what we have not built yet
        for name, caps in _repo_capabilities(db).items():
            repo.setdefault(name, []).extend(caps)
    providers = _provider_map(u)
    missing = []
    for base in sorted(selected):
        for output_name, output in u[base]["outputs"].items():
            for field in ("depends_exprs", "makedepends_exprs"):
                for expr in output.get(field, ()):
                    edge = _resolve_provider(providers, expr)
                    if field == "depends_exprs" and edge and (base, edge[0]) in policy:
                        continue
                    name, op, version = _split_expr(expr)
                    if any(c[0] in selected and _version_matches(c[2], op, version) for c in providers.get(name, ())):
                        continue                                  # any provider built in this run (e.g. a seed)
                    if any(_version_matches(cap_version, op, version)
                           for cap_name, cap_version in repo.get(name, ()) if cap_name == name):
                        continue
                    missing.append(f"{base}/{output_name}: {expr}")
    if missing:  # all of them at once: one fix round instead of one per miss
        raise SchedulerError("preflight missing retained provider: " + "; ".join(missing))
    return sorted(policy)


def demoted_exprs(u, graph, edges, targets):
    policy = validate_policy(u, graph, edges, set(targets))
    return sorted(f"{source}\t{record['expr']}" for source, target in policy
                  for record in edges[(source, target)] if record["field"] == "depends")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    try:
        u = universe()
        g, edges = build_edges(u)

        if cmd == "graph":
            for name in sorted(g):
                print(f"{name}: {' '.join(sorted(g[name]))}")
            return 0

        if cmd == "policy":
            wanted = _resolve_targets(u, sys.argv[2:], default_all=True)
            for source, target in validate_policy(u, g, edges, wanted):
                print(f"{source}>{target}")
            return 0

        if cmd == "demoted":
            wanted = _resolve_targets(u, sys.argv[2:])
            for expr in demoted_exprs(u, g, edges, wanted):
                print(expr)
            return 0

        if cmd == "preflight":
            repo_path = os.environ.get("KBUILD_REPO", f"{ROOT}/repos/candidate")
            args = sys.argv[2:]
            if args[:1] == ["--repo"]:
                if len(args) < 2:
                    raise SchedulerError("preflight: missing --repo path or targets")
                repo_path, args = args[1], args[2:]
            wanted = _resolve_targets(u, args, default_all=not args)
            for source, target in preflight(u, g, edges, wanted, os.path.join(repo_path, "kashira.db.tar.gz")):
                print(f"{source}>{target}")
            return 0

        if cmd == "plan":
            wanted = _resolve_targets(u, sys.argv[2:])
            order, policy = topo_order(u, g, edges, wanted)
            for source, target in policy:
                print(f"bootstrap-edge: {source} -> {target}", file=sys.stderr)
            for name in order:
                print(u[name]["pkgdir"])
            return 0

        if cmd == "wave":
            if len(sys.argv) < 3:
                print("wave: missing output file", file=sys.stderr)
                return 2
            outfile = sys.argv[2]
            wanted = _resolve_targets(u, sys.argv[3:])
            order, policy = topo_order(u, g, edges, wanted)
            with open(outfile, "w", encoding="utf-8") as stream:
                stream.write("# generated by kbuild-sched\n")
                for name in order:
                    stream.write(u[name]["pkgdir"] + "\n")
            print(f"wave: {len(order)} pkgbases -> {outfile} (bootstrap edges: {len(policy)})")
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
            _order, levels, policy = _topology(u, g, edges, wanted, _build_repo())
            for source, target in policy:
                print(f"bootstrap-edge: {source} -> {target}", file=sys.stderr)
            for members in levels:
                print(" ".join(members))
            return 0

        if cmd == "stage1":
            # "<pkgbase>\t<dependency expr>" per dependency a stage-1 build goes without (cycle among unbuilt targets)
            wanted = set(_resolve_targets(u, sys.argv[2:], default_all=True))
            g, edges = _retarget(u, g, edges, wanted)
            ignored = set(validate_policy(u, g, edges, wanted))
            _soft, stage1 = stage1_cuts(u, g, edges, wanted, ignored, _build_repo())
            for base in sorted(stage1):
                for target in sorted(stage1[base]):
                    for record in edges[(base, target)]:
                        print(f"{base}\t{record['expr']}")
            return 0

        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    except (OSError, SchedulerError) as exc:
        print(f"kbuild-sched: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
