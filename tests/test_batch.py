import copy
import json
import unittest

from app import Telemetry


class Clock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return float(self.calls)


EVENTS = [
    {"op": "inc", "name": "hits"},
    {"op": "inc", "name": "hits", "value": 4, "labels": (("b", "2"), ("a", "1")), "service": "api"},
    {"op": "observe", "name": "lat", "value": 1.5},
    {"op": "observe", "name": "lat", "value": 2, "labels": (("a", "1"),), "service": "api"},
    {"op": "start", "span": "root"},
    {"op": "start", "span": "child", "parent": "root", "service": "api"},
    {"op": "finish", "span": "child", "error": "boom", "service": "api"},
    {"op": "finish", "span": "root"},
]


class BatchTest(unittest.TestCase):
    def test_equivalent_to_sequential_calls(self):
        clock_a, clock_b = Clock(), Clock()
        t_batch = Telemetry(clock_a)
        t_seq = Telemetry(clock_b)
        events = copy.deepcopy(EVENTS)
        self.assertIsNone(t_batch.batch(events))
        for e in copy.deepcopy(EVENTS):
            op = e.pop("op")
            getattr(t_seq, op)(**e)
        self.assertEqual(t_batch.snapshot(), t_seq.snapshot())
        self.assertEqual(t_batch.json(), t_seq.json())
        for status in ("open", "closed", "error"):
            self.assertEqual(t_batch.query(status), t_seq.query(status))
        self.assertEqual(t_batch.trace("root"), t_seq.trace("root"))
        # start/finish 各自只读一次 clock，次数与逐条调用一致
        self.assertEqual(clock_a.calls, clock_b.calls)
        self.assertEqual(clock_a.calls, 4)

    def test_batch_internal_span_and_empty_batch(self):
        t = Telemetry(Clock())
        t.batch([])
        t.batch(())
        self.assertEqual(t.snapshot(), {"counters": [], "samples": [], "spans": []})
        # 批次内 start 的跨度可被同批次 finish 结束
        t.batch([{"op": "start", "span": "s"}, {"op": "finish", "span": "s"}])
        self.assertEqual(len(t.query("closed")), 1)

    def test_events_container_and_shape_rejected(self):
        t = Telemetry()
        for bad in (None, "x", 42, {"op": "inc"}, [{"op": "inc", "name": "x"}],):
            if isinstance(bad, list):
                continue
            with self.assertRaises(ValueError):
                t.batch(bad)
        for bad_event in (None, "x", 1, ["op"], (("op", "inc"),)):
            with self.assertRaises(ValueError):
                t.batch([bad_event])
        # op 缺失 / 未知 / 非字符串
        for ev in ({"name": "x"}, {"op": "nope", "name": "x"}, {"op": 1, "name": "x"},
                   {"op": ["inc"], "name": "x"}):
            with self.assertRaises(ValueError):
                t.batch([ev])

    def test_unknown_field_and_missing_required(self):
        t = Telemetry()
        bad = [
            {"op": "inc", "name": "x", "bogus": 1},
            {"op": "observe", "name": "x", "value": 1, "parent": None},
            {"op": "start", "span": "s", "value": 1},
            {"op": "finish", "span": "s", "labels": ()},
            {"op": "inc"},                                  # 缺 name
            {"op": "observe", "name": "x"},                 # 缺 value
            {"op": "start"},                                # 缺 span
            {"op": "finish"},                               # 缺 span
        ]
        for ev in bad:
            with self.assertRaises(ValueError, msg=repr(ev)):
                t.batch([ev])

    def test_value_rule_violations(self):
        t = Telemetry()
        bad = [
            {"op": "inc", "name": "x", "service": ""},
            {"op": "inc", "name": "x", "labels": (("k", 1), ("k", 2))},
            {"op": "observe", "name": "x", "value": float("nan")},
            {"op": "observe", "name": "x", "value": "abc"},
            {"op": "start", "span": ["unhashable"]},
            {"op": "finish", "span": "ghost"},
        ]
        for ev in bad:
            with self.assertRaises(ValueError, msg=repr(ev)):
                t.batch([ev])
        t.batch([{"op": "start", "span": "s"}])
        with self.assertRaises(ValueError):  # 重复开始
            t.batch([{"op": "start", "span": "s"}])
        t.batch([{"op": "finish", "span": "s"}])
        with self.assertRaises(ValueError):  # 已结束
            t.batch([{"op": "finish", "span": "s"}])
        with self.assertRaises(ValueError):  # 批次内重复开始
            t.batch([{"op": "start", "span": "a"}, {"op": "start", "span": "a"}])

    def test_atomic_rejection_no_clock_no_state_change(self):
        clock = Clock()
        t = Telemetry(clock)
        t.batch([{"op": "inc", "name": "keep"}, {"op": "start", "span": "s"}])
        before = (t.snapshot(), t.json(), clock.calls)
        bad_batches = [
            # 前面是合法事件（含 start，会读 clock），后面事件被拒绝
            [{"op": "start", "span": "new"}, {"op": "inc", "name": "x", "service": ""}],
            [{"op": "inc", "name": "y"}, {"op": "observe", "name": "z", "value": float("inf")}],
            [{"op": "start", "span": "new"}, {"op": "finish", "span": "ghost"}],
            [{"op": "inc", "name": "y"}, "not-an-event"],
        ]
        for batch in bad_batches:
            with self.assertRaises(ValueError):
                t.batch(batch)
            self.assertEqual(t.snapshot(), before[0])
            self.assertEqual(t.json(), before[1])
            self.assertEqual(clock.calls, before[2])  # clock 一次都未被读取

    def test_clock_exception_propagates_and_restores(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("clock broke")
            return 100.0 + len(calls)

        t = Telemetry(flaky)
        before = t.snapshot()
        with self.assertRaises(RuntimeError) as ctx:
            t.batch([
                {"op": "inc", "name": "c"},
                {"op": "start", "span": "a"},
                {"op": "start", "span": "b"},
            ])
        self.assertEqual(str(ctx.exception), "clock broke")
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.spans, {})
        self.assertEqual(t.counters, {})

    def test_input_events_not_mutated(self):
        events = copy.deepcopy(EVENTS)
        frozen = copy.deepcopy(events)
        t = Telemetry(Clock())
        t.batch(events)
        self.assertEqual(events, frozen)
        # 拒绝路径同样不改写
        bad = [{"op": "inc", "name": "x", "labels": [["k", "v"]]}, {"op": "bad"}]
        frozen_bad = copy.deepcopy(bad)
        with self.assertRaises(ValueError):
            t.batch(bad)
        self.assertEqual(bad, frozen_bad)

    def test_json_roundtrip_after_batch(self):
        t = Telemetry(Clock())
        t.batch(EVENTS)
        restored = Telemetry.from_snapshot(t.json())
        self.assertEqual(restored.snapshot(), t.snapshot())
        other = Telemetry(Clock())
        other.merge_snapshot(t.snapshot())
        self.assertEqual(other.json(), t.json())


if __name__ == "__main__":
    unittest.main()
