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

Cycle handling: cycles require explicit KBUILD_BOOTSTRAP_EDGES runtime policy;
unapproved cycles fail with typed dependency edges.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HEART = os.environ.get("KBUILD_HEART", "/mnt/lfs/src/kashira/pkgs")
FORKS = os.environ.get("KBUILD_FORKS", "/mnt/lfs/src/kashira-arch/pkgs")


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
    if isinstance(ent, dict) and ent.get("version") == 7 and ent.get("stamp") == stamp:
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
        _save_cache(_SRCINFO_CACHE_DATA)
        raise
    _SRCINFO_CACHE_DATA[pkgdir] = {
        "version": 7,
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


def _provider_map(u):
    providers = {}
    for base, info in u.items():
        for pkgname, output in info["outputs"].items():
            capabilities = [(pkgname, output["version"])]
            capabilities.extend((_split_expr(expr)[0], _split_expr(expr)[2])
                                for expr in output.get("provides_exprs", ()))
            for capability, version in capabilities:
                providers.setdefault(capability, []).append((base, pkgname, version))
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
            raise SchedulerError(f"bootstrap edge {source}>{target}: outside selected run")
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
    return policy


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


def _topology(u, graph, edges, subset=None):
    """Return deps-first order after removing only validated runtime policy edges."""
    nodes = set(graph) if subset is None else set(subset)
    policy = validate_policy(u, graph, edges, nodes)
    ignored = set(policy)
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


def preflight(u, graph, edges, targets, repo_path):
    selected = set(targets)
    policy = set(validate_policy(u, graph, edges, selected))
    repo = _repo_capabilities(repo_path)
    providers = _provider_map(u)
    for base in sorted(selected):
        for output_name, output in u[base]["outputs"].items():
            for field in ("depends_exprs", "makedepends_exprs"):
                for expr in output.get(field, ()):
                    edge = _resolve_provider(providers, expr)
                    if field == "depends_exprs" and edge and (base, edge[0]) in policy:
                        continue
                    if edge and edge[0] in selected:
                        continue
                    name, op, version = _split_expr(expr)
                    if any(_version_matches(cap_version, op, version)
                           for cap_name, cap_version in repo.get(name, ()) if cap_name == name):
                        continue
                    raise SchedulerError(f"preflight missing retained provider: {base}/{output_name}: {expr}")
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
            repo_path = os.environ.get("KBUILD_REPO", "/mnt/lfs/var/cache/pacman/kashira")
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
            _order, levels, policy = _topology(u, g, edges, wanted)
            for source, target in policy:
                print(f"bootstrap-edge: {source} -> {target}", file=sys.stderr)
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
