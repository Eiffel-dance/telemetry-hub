# -*- coding: utf-8 -*-
"""telemetry.span(...) 上下文入口的行为测试。

覆盖：进入/退出与 start/finish 等价、异常对象落库与传播、空块闭合、
嵌套与跨 service、显式 parent 不自动改写、同名跨 service 隔离、
scope 只读状态及副本隔离、重复使用/二次结束 ValueError 且不读第二次
clock、非法输入与容量拒绝不留记录不读 clock、clock 异常原样传播并
保留 finish 前状态，以及与 query/trace/快照/耗时查询/batch/restore/
merge/digest 的一致性与快照字段不变。
"""
import json
import unittest

import app as appmod
from app import Telemetry, TelemetryCapacityError


class SpanContextBasicTest(unittest.TestCase):
    def test_empty_body_creates_closed_span(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a") as scope:
            self.assertEqual(scope.span, "a")
            self.assertEqual(scope.service, "")
            self.assertIsNone(scope.parent)
            self.assertEqual(scope.start, 0)
            self.assertIsNone(scope.end)
            self.assertIsNone(scope.error)
        self.assertEqual(scope.start, 0)
        self.assertEqual(scope.end, 1)
        self.assertIsNone(scope.error)
        self.assertEqual([e["span"] for e in t.query("open")], [])
        self.assertEqual([e["span"] for e in t.query("closed")], ["a"])

    def test_one_clock_read_each_for_start_and_finish_in_order(self):
        calls = []

        def clock():
            calls.append(len(calls))
            return calls[-1]

        t = Telemetry(clock)
        with t.span("a"):
            self.assertEqual(calls, [0])
        self.assertEqual(calls, [0, 1])
        with t.span("b"):
            pass
        self.assertEqual(calls, [0, 1, 2, 3])

    def test_explicit_service_parent_and_labels(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a", service="api", parent="root",
                    labels=(("b", 2), ("a", 1))) as scope:
            self.assertEqual(scope.service, "api")
            self.assertEqual(scope.parent, "root")
            self.assertEqual(scope.labels, [("a", 1), ("b", 2)])
        record = t.spans[("api", "a")]
        self.assertEqual(record["parent"], "root")
        self.assertEqual(record["labels"], (("a", 1), ("b", 2)))

    def test_unlabeled_scope_labels_is_none(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a") as scope:
            self.assertIsNone(scope.labels)

    def test_normal_exit_sets_error_none(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a"):
            pass
        self.assertIsNone(t.spans[("", "a")]["error"])

    def test_same_span_name_isolated_per_service(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("z", service="a"), t.span("z", service="b"):
            pass
        self.assertEqual(len(t.spans), 2)
        self.assertEqual(
            [e["service"] for e in t.query("closed")], ["a", "b"]
        )


class SpanContextExceptionTest(unittest.TestCase):
    def test_exception_object_stored_then_propagated(self):
        t = Telemetry(iter(range(100)).__next__)

        class Boom(Exception):
            pass

        err = Boom("boom")
        scope = None
        with self.assertRaises(Boom) as caught:
            with t.span("a") as scope:
                raise err
        self.assertIs(caught.exception, err)
        record = t.spans[("", "a")]
        self.assertIs(record["error"], err)
        self.assertIsNotNone(record["end"])
        self.assertIs(scope.error, err)
        self.assertEqual([e["span"] for e in t.query("error")], ["a"])
        self.assertIn("a", [e["span"] for e in t.query("closed")])

    def test_exit_returns_false(self):
        t = Telemetry(iter(range(100)).__next__)
        manager = t.span("a")
        scope = manager.__enter__()
        err = RuntimeError("x")
        self.assertIs(manager.__exit__(type(err), err, None), False)
        self.assertIs(t.spans[("", "a")]["error"], err)

    def test_nested_contexts_both_capture_same_exception(self):
        t = Telemetry(iter(range(100)).__next__)

        class E(Exception):
            pass

        err = E("x")
        with self.assertRaises(E):
            with t.span("outer"):
                with t.span("inner"):
                    raise err
        self.assertIs(t.spans[("", "inner")]["error"], err)
        self.assertIs(t.spans[("", "outer")]["error"], err)
        # 外层正常结束的兄弟跨度不受影响。
        t2 = Telemetry(iter(range(100)).__next__)
        with self.assertRaises(E):
            with t2.span("outer"):
                with t2.span("ok"):
                    pass
                with t2.span("bad"):
                    raise err
        self.assertIsNone(t2.spans[("", "ok")]["error"])
        self.assertIs(t2.spans[("", "bad")]["error"], err)
        self.assertIs(t2.spans[("", "outer")]["error"], err)

    def test_json_placeholder_rule_unchanged(self):
        t = Telemetry(iter(range(100)).__next__)
        with self.assertRaises(RuntimeError):
            with t.span("a"):
                raise RuntimeError("nope")
        payload = json.loads(t.json())
        self.assertEqual(
            payload["spans"][0]["error"],
            {"type": "RuntimeError", "message": "nope"},
        )

    def test_strict_json_error_roundtrip(self):
        # 代码块抛字符串不可行（raise 必须是异常），这里验证上下文记录的
        # 异常经 json 占位后可恢复，恢复入口与既有规则逐项一致。
        t = Telemetry(iter(range(100)).__next__)
        with self.assertRaises(RuntimeError):
            with t.span("a"):
                raise RuntimeError("nope")
        restored = Telemetry.restore(t.json())
        self.assertEqual(restored.json(), t.json())


class SpanContextNestingTest(unittest.TestCase):
    def test_nesting_and_cross_service_without_auto_parent(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("outer", service="api"):
            with t.span("child", parent="outer", service="api"):
                pass
            with t.span("stranger", service="web"):
                pass
        tree = t.trace("outer", service="api")
        self.assertEqual([n["span"] for n in tree["children"]], ["child"])
        # 跨 service 的跨度 parent 未显式给出，不自动挂接。
        self.assertIsNone(t.spans[("web", "stranger")]["parent"])
        self.assertEqual(t.trace("stranger", service="web")["children"], [])

    def test_nested_clocks_interleave(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a"):
            with t.span("b"):
                pass
        a = t.spans[("", "a")]
        b = t.spans[("", "b")]
        self.assertEqual((a["start"], b["start"], b["end"], a["end"]),
                         (0, 1, 2, 3))


class SpanContextValidationTest(unittest.TestCase):
    def _failing_clock(self):
        def clock():
            raise AssertionError("clock must not be called")
        return clock

    def test_duplicate_span_rejected_without_record_or_clock(self):
        calls = []

        def clock():
            calls.append(1)
            return len(calls)

        t = Telemetry(clock)
        t.start("dup")
        self.assertEqual(calls, [1])
        with self.assertRaises(ValueError):
            with t.span("dup"):
                self.fail("body must not run")
        self.assertIsNone(t.spans[("", "dup")]["end"])
        # 已结束的同标识跨度同样拒绝。
        t.finish("dup")
        self.assertEqual(len(calls), 2)
        with self.assertRaises(ValueError):
            with t.span("dup"):
                pass
        # 重复开始一律不读 clock。
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(t.spans), 1)

    def test_invalid_service_span_labels_rejected_before_clock(self):
        t = Telemetry(self._failing_clock())

        class Obj:
            pass

        cases = [
            lambda: t.span("s", service=1),
            lambda: t.span("s", service=""),
            lambda: t.span(["s"]),
            lambda: t.span({"s": 1}),
            lambda: t.span("s1", labels=123),
            lambda: t.span("s2", labels=(("k", 1), ("k", 2))),
            lambda: t.span("s3", labels=(("k", Obj()),)),
            lambda: t.span("s4", labels=(123,)),
        ]
        for make in cases:
            manager = make()
            with self.assertRaises(ValueError):
                manager.__enter__()
        self.assertEqual(t.spans, {})

    def test_failed_enter_consumes_context_without_record(self):
        t = Telemetry(iter(range(100)).__next__)
        manager = t.span("a", service=1)
        with self.assertRaises(ValueError):
            manager.__enter__()
        # start 失败后退出不得补一条 finish，也不读 clock。
        with self.assertRaises(ValueError):
            manager.__exit__(None, None, None)
        self.assertEqual(t.spans, {})
        scope = t.span("pre")
        with self.assertRaises(ValueError):
            scope.start

    def test_span_capacity_exceeded(self):
        t = Telemetry(iter(range(100)).__next__, max_spans=1)
        t.start("one")
        with self.assertRaises(TelemetryCapacityError):
            with t.span("two"):
                pass
        self.assertNotIn(("", "two"), t.spans)

    def test_spans_do_not_consume_series_capacity(self):
        t = Telemetry(iter(range(100)).__next__, max_series=0)
        with t.span("a"):
            pass
        with self.assertRaises(TelemetryCapacityError):
            t.inc("c")

    def test_caller_label_object_not_mutated(self):
        t = Telemetry(iter(range(100)).__next__)
        labels = [("z", 1), ("a", 2)]
        with t.span("a", labels=labels):
            pass
        self.assertEqual(labels, [("z", 1), ("a", 2)])


class SpanContextReuseTest(unittest.TestCase):
    def test_reenter_same_object_raises(self):
        t = Telemetry(iter(range(100)).__next__)
        manager = t.span("a")
        with manager:
            pass
        with self.assertRaises(ValueError):
            with manager:
                pass
        self.assertEqual(len(t.spans), 1)

    def test_double_exit_raises_without_second_timestamp(self):
        t = Telemetry(iter(range(100)).__next__)
        manager = t.span("a")
        manager.__enter__()
        manager.__exit__(None, None, None)
        end = t.spans[("", "a")]["end"]
        self.assertEqual(end, 1)
        with self.assertRaises(ValueError):
            manager.__exit__(None, None, None)
        self.assertEqual(t.spans[("", "a")]["end"], 1)

    def test_exit_without_enter_raises(self):
        t = Telemetry(iter(range(100)).__next__)
        manager = t.span("a")
        with self.assertRaises(ValueError):
            manager.__exit__(None, None, None)
        self.assertEqual(t.spans, {})

    def test_external_finish_then_context_exit_raises_no_new_timestamp(self):
        t = Telemetry(iter(range(100)).__next__)
        manager = t.span("a")
        manager.__enter__()
        t.finish("a")  # 外部先结束
        end = t.spans[("", "a")]["end"]
        with self.assertRaises(ValueError):
            manager.__exit__(None, None, None)
        self.assertEqual(t.spans[("", "a")]["end"], end)


class SpanContextClockFailureTest(unittest.TestCase):
    def test_clock_failure_on_enter_leaves_no_record(self):
        def clock():
            raise RuntimeError("clock down")

        t = Telemetry(clock)
        manager = t.span("a")
        with self.assertRaises(RuntimeError):
            manager.__enter__()
        self.assertEqual(t.spans, {})

    def test_clock_failure_on_finish_propagates_and_preserves_state(self):
        sequence = iter([10, 20])

        class ClockErr(RuntimeError):
            pass

        def clock():
            value = next(sequence)
            if value == 20:
                raise ClockErr("clock down")
            return value

        t = Telemetry(clock)
        manager = t.span("a")
        manager.__enter__()
        with self.assertRaises(ClockErr):
            manager.__exit__(None, None, None)
        record = t.spans[("", "a")]
        self.assertEqual(record["start"], 10)
        self.assertIsNone(record["end"])
        self.assertIsNone(record["error"])
        self.assertEqual([e["span"] for e in t.query("open")], ["a"])
        # clock 失败后该上下文仍视为已消耗，再次退出不读 clock。
        with self.assertRaises(ValueError):
            manager.__exit__(None, None, None)

    def test_clock_failure_on_finish_with_body_exception(self):
        class BodyErr(Exception):
            pass

        class ClockErr(RuntimeError):
            pass

        sequence = iter([1, 2])

        def clock():
            value = next(sequence)
            if value == 2:
                raise ClockErr("clock down")
            return value

        t = Telemetry(clock)
        manager = t.span("a")
        manager.__enter__()
        # clock 异常原样传播（取代代码块异常）；记录仍保持 finish 前状态。
        with self.assertRaises(ClockErr):
            manager.__exit__(BodyErr, BodyErr("body"), None)
        record = t.spans[("", "a")]
        self.assertIsNone(record["end"])
        self.assertIsNone(record["error"])


class SpanContextScopeIsolationTest(unittest.TestCase):
    def test_scope_values_are_independent_snapshots(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a", parent=["p"], labels=(("k", ["v"]),)) as scope:
            parent = scope.parent
            labels = scope.labels
        parent.append("q")
        labels[0][1].append("w")
        self.assertEqual(t.spans[("", "a")]["parent"], ["p"])
        self.assertEqual(scope.parent, ["p"])
        self.assertEqual(scope.labels, [("k", ["v"])])
        # 连续读取得到不同副本。
        self.assertIsNot(scope.parent, scope.parent)

    def test_scope_readable_after_exit_reflects_closed_state(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a") as scope:
            pass
        self.assertEqual(scope.start, 0)
        self.assertEqual(scope.end, 1)
        self.assertIsNone(scope.error)

    def test_mutating_scope_does_not_write_back(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("a") as scope:
            entry = scope._record()
            entry["span"] = "hacked"
            entry["end"] = 99
            # 块内聚合器记录仍为 open，改写返回副本不回写。
            internal = t.spans[("", "a")]
            self.assertEqual(internal["start"], 0)
            self.assertIsNone(internal["end"])
        # 退出后聚合器按真实 finish 闭合，内部标识仍是 "a"。
        self.assertEqual(scope.span, "a")
        self.assertEqual(scope.end, 1)


class SpanContextEquivalenceTest(unittest.TestCase):
    def _build_context(self):
        t = Telemetry(iter(range(50)).__next__)
        with t.span("root", service="api", labels=(("z", 1), ("a", 2))):
            with t.span("a", parent="root", service="api"):
                pass
            try:
                with t.span("b", parent="root", service="api"):
                    raise ValueError("boom")
            except ValueError:
                pass
        t.inc("hits", 3)
        t.observe("lat", 1.5, service="api")
        return t

    def _build_manual(self):
        t = Telemetry(iter(range(50)).__next__)
        t.start("root", service="api", labels=(("z", 1), ("a", 2)))
        t.start("a", parent="root", service="api")
        t.finish("a", service="api")
        t.start("b", parent="root", service="api")
        t.finish("b", service="api", error=ValueError("boom"))
        t.finish("root", service="api")
        t.inc("hits", 3)
        t.observe("lat", 1.5, service="api")
        return t

    @staticmethod
    def _placeholder_errors(records):
        out = []
        for record in records:
            record = dict(record)
            error = record.get("error")
            if error is not None and not isinstance(
                error, (str, int, float, bool, list, dict)
            ):
                record["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
            out.append(record)
        return out

    def test_json_snapshot_digest_identical(self):
        a, b = self._build_context(), self._build_manual()
        self.assertEqual(a.json(), b.json())
        self.assertEqual(a.digest(), b.digest())
        self.assertEqual(
            self._placeholder_errors(a.snapshot()["spans"]),
            self._placeholder_errors(b.snapshot()["spans"]),
        )
        self.assertEqual(a.snapshot()["counters"], b.snapshot()["counters"])
        self.assertEqual(a.snapshot()["samples"], b.snapshot()["samples"])

    def test_query_trace_duration_queries_equivalent(self):
        a, b = self._build_context(), self._build_manual()
        for status in ("open", "closed", "error"):
            self.assertEqual(
                self._placeholder_errors(a.query(status)),
                self._placeholder_errors(b.query(status)),
            )
        ta = a.trace("root", service="api")
        tb = b.trace("root", service="api")
        ta["children"][1]["error"] = tb["children"][1]["error"] = None
        self.assertEqual(ta, tb)
        self.assertEqual(
            a.span_duration_stats(service="api"),
            b.span_duration_stats(service="api"),
        )
        self.assertEqual(
            [r["span"] for r in a.spans_by_duration()],
            [r["span"] for r in b.spans_by_duration()],
        )
        self.assertEqual(
            self._placeholder_errors(a.spans_by_start_time()),
            self._placeholder_errors(b.spans_by_start_time()),
        )
        self.assertEqual(
            [s["span"] for s in a.trace_critical_path("root", service="api")["spans"]],
            [s["span"] for s in b.trace_critical_path("root", service="api")["spans"]],
        )

    def test_restore_merge_diff_and_resume(self):
        a = self._build_context()
        text = a.json()
        restored = Telemetry.restore(text)
        self.assertEqual(restored.digest(), a.digest())
        merged = Telemetry(iter(range(5)).__next__)
        merged.merge_snapshot(text)
        self.assertEqual(merged.json(), a.json())
        diff = Telemetry.diff_snapshots(text, text)
        for section in diff.values():
            for rows in section.values():
                self.assertEqual(rows, [])
        self.assertTrue(Telemetry.verify_digest(text, a.digest()))
        # 恢复出的 open 跨度拒绝被上下文重复开始，但可继续 finish。
        open_t = Telemetry(iter([7]).__next__)
        open_t.start("op")
        resumed = Telemetry.restore(open_t.snapshot(),
                                   clock=iter([9]).__next__)
        with self.assertRaises(ValueError):
            with resumed.span("op"):
                pass
        resumed.finish("op")
        self.assertEqual(resumed.query("closed")[0]["end"], 9)

    def test_batch_interoperates_in_both_directions(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("early"):
            pass
        t.batch([
            {"op": "inc", "name": "x"},
            {"op": "start", "span": "late", "parent": "early"},
        ])
        self.assertEqual([e["span"] for e in t.query("open")], ["late"])
        t.batch([{"op": "finish", "span": "late"}])
        # 上下文打开的跨度可由 batch 结束，之后上下文退出按二次结束拒绝。
        manager = t.span("via")
        manager.__enter__()
        t.batch([{"op": "finish", "span": "via"}])
        with self.assertRaises(ValueError):
            manager.__exit__(None, None, None)
        self.assertIsNotNone(t.spans[("", "via")]["end"])

    def test_snapshot_fields_unchanged(self):
        a = self._build_context()
        for entry in a.snapshot()["spans"]:
            self.assertLessEqual(
                set(entry),
                {"span", "service", "parent", "start", "end", "error", "labels"},
            )
        self.assertEqual(
            set(a.snapshot()), {"counters", "samples", "spans"}
        )
        # 紧凑 JSON 与稳定键序不变。
        text = a.json()
        self.assertNotIn(": ", text)
        self.assertNotIn(", ", text)
        self.assertEqual(text, json.dumps(
            json.loads(text), sort_keys=True, separators=(",", ":")
        ))

    def test_no_new_network_modules(self):
        import inspect
        source = inspect.getsource(appmod)
        for forbidden in ("socket", "urllib", "requests", "http.client"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
