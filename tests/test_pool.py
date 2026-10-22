import importlib.machinery
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("kbuild_pool", os.path.join(HERE, "..", "kbuild-pool"))
spec = importlib.util.spec_from_loader("kbuild_pool", loader)
kp = importlib.util.module_from_spec(spec)
loader.exec_module(kp)


class Budget(kp.SimRunner):
    """SimRunner that asserts the pool never oversubscribes CPU or memory."""

    def __init__(self, pool_ref, *a, **kw):
        super().__init__(*a, **kw)
        self.pool_ref = pool_ref

    def start(self, job):
        pool = self.pool_ref[0]
        cpus = sum(len(j.cpus) for j in pool.running.values())
        assert cpus <= kp.CPU_BUDGET, cpus
        assert pool.mem_used <= kp.MEM_BUDGET, pool.mem_used
        assert len({c for j in pool.running.values() for c in j.cpus}) == cpus, "cpusets overlap"
        heavy = [j for j in pool.running.values() if j.cls == "heavy"]
        assert not heavy or len(pool.running) == 1, "heavy is not exclusive"
        super().start(job)


def make(specs, deps=None, durations=None, outcome=None, standin=True):
    durations = durations or {}
    dur = lambda n: durations.get(n, 10)  # noqa: E731
    jobs = [kp.Job(n, cls, "A", knobs) for n, cls, knobs in specs]
    ref = [None]
    runner = Budget(ref, dur, outcome)
    pool = kp.Pool(jobs, deps or {}, dur, runner, standin=standin, log=lambda m: None)
    ref[0] = pool
    return pool, runner


def starts(pool):
    return [n for ev, n, *_ in pool.history if ev == "start"]


class PoolTest(unittest.TestCase):
    def test_budget_never_exceeded(self):
        specs = [(f"s{i}", "small", []) for i in range(20)] + [(f"m{i}", "medium", []) for i in range(6)] \
            + [("l", "large", []), ("h", "heavy", [])]
        pool, _ = make(specs)
        self.assertTrue(all(v == "built" for v in pool.run().values()))

    def test_heavy_reserves_machine(self):
        # h is the longest critical path: once it is the head, nothing else starts until it ran alone
        pool, _ = make([("h", "heavy", []), ("a", "small", []), ("b", "small", [])], durations={"h": 100})
        pool.run()
        self.assertEqual(starts(pool)[0], "h")

    def test_dependents_run_after_failed_dep(self):
        fail = lambda j: (1, "==> ERROR: Failure while downloading x") if j.name == "a" else (0, "")  # noqa: E731
        pool, _ = make([("a", "small", []), ("b", "small", [])], deps={"b": {"a"}}, outcome=fail)
        res = pool.run()
        self.assertEqual(res, {"a": "fail:source", "b": "built"})
        self.assertLess(starts(pool).index("a"), starts(pool).index("b"))

    def test_failed_dep_without_standins_fails_dependent(self):
        fail = lambda j: (1, "unable to satisfy dependency 'x'") if j.name == "a" else (0, "")  # noqa: E731
        pool, _ = make([("a", "small", []), ("b", "small", [])], deps={"b": {"a"}}, outcome=fail, standin=False)
        self.assertEqual(pool.run(), {"a": "fail:deps", "b": "fail:dep"})

    def test_tier_ladder(self):
        tiers = []

        def outcome(j):
            tiers.append(j.tier)
            return (0, "") if j.tier == 2 else (1, "foo.c:1: error: bad")

        pool, _ = make([("a", "small", [])], outcome=outcome)
        self.assertEqual(pool.run(), {"a": "built"})
        self.assertEqual(tiers, [0, 1, 2])
        self.assertEqual(pool.jobs["a"].tier0_error, "foo.c:1: error: bad")

    def test_build_fails_after_gcc(self):
        pool, _ = make([("a", "small", [])], outcome=lambda j: (1, "error: x"))
        self.assertEqual(pool.run(), {"a": "fail:build"})

    def test_oom_escalates_one_class(self):
        seen = []

        def outcome(j):
            seen.append(j.cls)
            return (137, "Killed") if j.cls == "small" else (0, "")

        pool, _ = make([("a", "small", [])], outcome=outcome)
        self.assertEqual(pool.run(), {"a": "built"})
        self.assertEqual(seen, ["small", "medium"])

    def test_critical_path_first(self):
        # c heads a chain c -> x -> y; b is alone: c starts first though b sorts first by name
        specs = [("b", "small", []), ("c", "small", []), ("x", "small", []), ("y", "small", [])]
        pool, _ = make(specs, deps={"x": {"c"}, "y": {"x"}})
        kp.CPU_BUDGET, old = 2, kp.CPU_BUDGET
        try:
            pool.free_cpus = [0, 1]
            pool.run()
        finally:
            kp.CPU_BUDGET = old
        self.assertEqual(starts(pool)[0], "c")

    def test_cycle_is_cut(self):
        pool, _ = make([("a", "small", []), ("b", "small", [])], deps={"a": {"b"}, "b": {"a"}})
        self.assertEqual(pool.run(), {"a": "built", "b": "built"})

    def test_classify(self):
        self.assertEqual(kp.classify(1, "error: target not found: foo"), "deps")
        self.assertEqual(kp.classify(137, ""), "oom")
        self.assertEqual(kp.classify(2, "c++: fatal error: Killed signal terminated program cc1plus"), "oom")
        self.assertEqual(kp.classify(2, "x.c:3:1: error: y"), "build")


if __name__ == "__main__":
    unittest.main()
