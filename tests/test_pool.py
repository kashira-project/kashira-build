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

    def test_build_failure_is_final(self):
        tries = []
        pool, _ = make([("a", "small", [])], outcome=lambda j: (tries.append(j.tier), (1, "error: x"))[1])
        self.assertEqual(pool.run(), {"a": "fail:build"})
        self.assertEqual(tries, [0])

    def test_oom_escalates_one_class(self):
        seen = []

        def outcome(j):
            seen.append(j.cls)
            return (137, "Killed") if j.cls == "small" else (0, "")

        pool, _ = make([("a", "small", [])], outcome=outcome)
        self.assertEqual(pool.run(), {"a": "built"})
        self.assertEqual(seen, ["small", "medium"])

    def test_heavy_oom_gets_more_memory(self):
        mems = []

        def outcome(j):
            mems.append(j.spec()[1])
            return (137, "Killed") if len(mems) == 1 else (0, "")

        pool, _ = make([("h", "heavy", [])], outcome=outcome)
        self.assertEqual(pool.run(), {"h": "built"})
        self.assertEqual(mems, [14, 20])

    def test_backfill_before_heavy(self):
        # after r: 4 CPUs free until m ends at 100; h (heavy, 8) waits, short s backfills, long l (8) exceeds the spare 4
        after_r = {"h": {"r"}, "s": {"r"}, "l": {"r"}}
        pool, _ = make([("m", "large", []), ("q", "medium", []), ("r", "medium", []), ("h", "heavy", []),
                        ("s", "small", []), ("l", "large", [])], deps=after_r,
                       durations={"m": 100, "q": 200, "r": 5, "h": 500, "s": 10, "l": 300})
        kp.MEM_BUDGET, old = 64, kp.MEM_BUDGET    # CPUs only: m, q and r start together
        try:
            pool.run()
        finally:
            kp.MEM_BUDGET = old
        order = starts(pool)
        self.assertLess(order.index("s"), order.index("h"))
        self.assertLess(order.index("h"), order.index("l"))

    def test_backfill_in_spare(self):
        # h needs 8 CPUs when m (8) ends at 100; q ends at 100 too, so 4 CPUs are spare then: long l still starts
        after_r = {"h": {"r"}, "l": {"r"}}
        pool, _ = make([("m", "large", []), ("q", "medium", []), ("r", "medium", []), ("h", "heavy", []),
                        ("l", "medium", [])], deps=after_r, durations={"m": 100, "q": 100, "r": 5, "h": 500, "l": 300})
        kp.MEM_BUDGET, old = 64, kp.MEM_BUDGET
        try:
            pool.run()
        finally:
            kp.MEM_BUDGET = old
        order = starts(pool)
        self.assertLess(order.index("l"), order.index("h"))

    def test_small_shares_cpu_when_host_idles(self):
        class Idle(kp.SimRunner):
            def spare(self):
                return True
        jobs = [kp.Job(n, "small", "A", []) for n in "abc"]
        runner = Idle(lambda n: 10)
        pool = kp.Pool(jobs, {}, lambda n: 10, runner, log=lambda m: None)
        kp.CPU_BUDGET, old = 1, kp.CPU_BUDGET
        try:
            pool.free_cpus = [0]
            self.assertEqual(pool.run(), {n: "built" for n in "abc"})
        finally:
            kp.CPU_BUDGET = old
        self.assertEqual(len(runner.ends), 0)
        self.assertEqual(starts(pool), ["a", "b", "c"])
        self.assertEqual(pool.free_cpus, [0])       # shared jobs never return CPUs they did not own

    def test_requeue_failed(self):
        pool, _ = make([("a", "small", [])], outcome=lambda j: (1, "==> ERROR: Failure while downloading x"))
        self.assertEqual(pool.run(), {"a": "fail:source"})
        pool.runner.outcome = lambda j: (0, "")
        pool.requeue(["a"], {"a": ["openssl3"]})
        self.assertEqual(pool.run(), {"a": "built"})
        self.assertEqual(pool.jobs["a"].knobs, ["openssl3"])

    def test_lone_job_takes_machine(self):
        # b waits on a: a starts alone and gets every CPU; b is alone too once a is done
        pool, _ = make([("a", "heavy", []), ("b", "small", [])], deps={"b": {"a"}})
        pool.run()
        self.assertEqual([c for ev, n, cls, t, c in pool.history if ev == "start"], [kp.CPU_BUDGET] * 2)

    def test_lone_job_shares_when_others_do_not_wait_on_it(self):
        # h is alone ready only because b waits on a (not on h): h takes its class CPUs, not the machine
        pool, _ = make([("h", "heavy", []), ("a", "small", []), ("b", "small", [])], deps={"b": {"a"}, "a": {"x"}},
                       durations={"h": 100})
        pool.jobs.pop("x", None)
        pool.run()
        first = [c for ev, n, cls, t, c in pool.history if ev == "start" and n == "h"][0]
        self.assertLess(first, kp.CPU_BUDGET)

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

    def test_reuse_runs_beside_heavy(self):
        # h (heavy) holds the machine; r only re-validates a recorded build: it starts at once, outside the budget
        pool, _ = make([("h", "heavy", []), ("a", "small", []), ("r", "small", [])], durations={"h": 100, "r": 100})
        pool.jobs["r"].reuse = True
        pool.run()
        ev = [(e, n) for e, n, *_ in pool.history]
        self.assertLess(ev.index(("start", "r")), ev.index(("built", "h")))  # r ran while h held the machine
        self.assertLess(ev.index(("start", "r")), ev.index(("start", "a")))

    def test_reuse_ignores_deps(self):
        pool, _ = make([("t", "heavy", []), ("r", "small", [])], deps={"r": {"t"}}, durations={"t": 100})
        pool.jobs["r"].reuse = True
        pool.run()
        ev = [(e, n) for e, n, *_ in pool.history]
        self.assertLess(ev.index(("start", "r")), ev.index(("built", "t")))

    def test_prebuilt_dep_does_not_gate(self):
        pool, _ = make([("g", "heavy", []), ("b", "small", [])], deps={"b": {"g"}}, durations={"g": 100})
        pool.prebuilt = {"g"}
        pool.run()
        ev = [(e, n) for e, n, *_ in pool.history]
        self.assertLess(ev.index(("start", "b")), ev.index(("built", "g")))

    def test_cycle_is_cut(self):
        pool, _ = make([("a", "small", []), ("b", "small", [])], deps={"a": {"b"}, "b": {"a"}})
        self.assertEqual(pool.run(), {"a": "built", "b": "built"})

    def test_classify(self):
        self.assertEqual(kp.classify(1, "error: target not found: foo"), "deps")
        self.assertEqual(kp.classify(137, ""), "oom")
        self.assertEqual(kp.classify(1, "FAILED: [code=255] tools/flang/OpenMP.cpp.o"), "oom")  # cgroup kill of a clang child
        self.assertEqual(kp.classify(2, "c++: fatal error: Killed signal terminated program cc1plus"), "oom")
        self.assertEqual(kp.classify(2, "x.c:3:1: error: y"), "build")
        self.assertEqual(kp.classify(101, "process didn't exit successfully: `rustc ...` (signal: 9, SIGKILL: kill)"), "oom")
        self.assertEqual(kp.classify(1, "error: failed retrieving file 'tzdata-1-x86_64.pkg.tar.zst' from disk : Exceeded"
                                        "\nerror: failed to commit transaction (download library error)"), "transient")
        self.assertEqual(kp.classify(1, "==> Finished making: z 1-1\nunexpected or stale archive: z"), "post")


if __name__ == "__main__":
    unittest.main()
