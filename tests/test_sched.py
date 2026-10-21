"""kbuild-sched cycle policy: repo-satisfied makedepends do not order a run; cycles among unbuilt targets get stage-1 cuts."""
import importlib.util, os, unittest

os.environ["KBUILD_BOOTSTRAP_EDGES"] = ""
_spec = importlib.util.spec_from_file_location("ks", os.path.join(os.path.dirname(__file__), "..", "kbuild-sched.py"))
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)


def universe(spec):
    """{base: (depends, makedepends)} -> kbuild-sched universe."""
    return {b: {"pkgbase": b, "pkgdir": f"/x/{b}", "outputs": {b: {
        "version": "1-1", "depends_exprs": list(d), "makedepends_exprs": list(m), "provides_exprs": []}}}
        for b, (d, m) in spec.items()}


def levels(u, repo):
    g, edges = ks.build_edges(u)
    return ks._topology(u, g, edges, set(u), repo)[1]


class T(unittest.TestCase):
    def test_repo_satisfied_makedepends_cycle_is_ordered_by_runtime_only(self):
        # a makedepends b (docs), b depends a: Arch's situation; b is already in the repo
        u = universe({"a": ((), ("b",)), "b": (("a",), ())})
        self.assertEqual(levels(u, {"b": [("b", "1-1")]}), [["a"], ["b"]])

    def test_cycle_among_unbuilt_targets_gets_one_stage1_member(self):
        u = universe({"a": ((), ("b",)), "b": ((), ("a", "c")), "c": ((), ())})
        g, edges = ks.build_edges(u)
        soft, stage1 = ks.stage1_cuts(u, g, edges, set(u), set(), {})
        self.assertEqual(soft, set())
        self.assertEqual(len(stage1), 1)                    # one makedepends edge cut breaks the cycle
        (n, cut), = stage1.items()
        self.assertEqual(cut, {"a" if n == "b" else "b"})

    def test_versioned_makedepends_needs_matching_repo_version(self):
        u = universe({"a": ((), ("b>=2",)), "b": ((), ("a",))})
        g, edges = ks.build_edges(u)
        u["b"]["outputs"]["b"]["version"] = "2-1"
        g, edges = ks.build_edges(u)
        soft, stage1 = ks.stage1_cuts(u, g, edges, set(u), set(), {"b": [("b", "1-1")]})   # repo b too old
        self.assertNotIn(("a", "b"), soft)
        self.assertTrue(stage1)

    def test_runtime_cycle_through_built_package_is_soft(self):
        u = universe({"x": ((), ("y",)), "y": (("x",), ())})          # x (in repo) makedepends new y; y depends x
        g, edges = ks.build_edges(u)
        soft, stage1 = ks.stage1_cuts(u, g, edges, set(u), set(), {"x": [("x", "1-1")]})
        self.assertEqual((soft, stage1), ({("y", "x")}, {}))

    def test_prefers_cutting_makedepends_over_depends(self):
        u = universe({"a": (("b",), ()), "b": ((), ("a",))})          # a depends b, b makedepends a
        g, edges = ks.build_edges(u)
        _soft, stage1 = ks.stage1_cuts(u, g, edges, set(u), set(), {})
        self.assertEqual(stage1, {"b": {"a"}})

    def test_without_repo_cycles_still_fail(self):
        u = universe({"a": ((), ("b",)), "b": ((), ("a",))})
        g, edges = ks.build_edges(u)
        with self.assertRaises(ks.SchedulerError):
            ks._topology(u, g, edges, set(u))

    def test_self_hosting_dep_orders_after_seed(self):
        u = universe({"ghc": ((), ("ghc",)), "ghc-bootstrap": ((), ())})
        u["ghc-bootstrap"]["outputs"]["ghc-bootstrap"]["provides_exprs"] = ["ghc=9.6.7"]
        g, _edges = ks.build_edges(u)
        self.assertEqual(g["ghc"], {"ghc-bootstrap"})


if __name__ == "__main__":
    unittest.main()
