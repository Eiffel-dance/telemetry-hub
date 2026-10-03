import copy
import json
import math
import unittest

import app as appmod
from app import Telemetry


class TelemetryBehaviorTest(unittest.TestCase):
    def test_service_isolation_and_default(self):
        t = Telemetry()
        t.inc("hits")
        t.inc("hits", service="api")
        t.inc("hits", service="web")
        snap = t.snapshot()
        values = {(c["service"], c["name"]): c["value"] for c in snap["counters"]}
        self.assertEqual(values, {("", "hits"): 1, ("api", "hits"): 1, ("web", "hits"): 1})
        self.assertEqual(snap["counters"][0]["service"], "")
        # 同标签不同服务分别聚合
        t.observe("lat", 1, labels=(("k", "v"),))
        t.observe("lat", 2, labels=(("k", "v"),), service="api")
        by_service = {s["service"]: s for s in t.snapshot()["samples"] if s["name"] == "lat"}
        self.assertEqual(by_service[""]["values"], [1])
        self.assertEqual(by_service["api"]["values"], [2])

    def test_invalid_service_rejected(self):
        t = Telemetry()
        for bad in ("", 1, b"x"):
            with self.assertRaises(ValueError):
                t.inc("x", service=bad)
            with self.assertRaises(ValueError):
                t.observe("x", 1, service=bad)
            with self.assertRaises(ValueError):
                t.start("x", service=bad)

    def test_label_normalization_duplicate_and_unserializable(self):
        t = Telemetry()
        t.inc("h", labels=(("a", "1"), ("b", "2")))
        t.inc("h", labels=(("b", "2"), ("a", "1")))
        counters = t.snapshot()["counters"]
        self.assertEqual(len(counters), 1)
        self.assertEqual(counters[0]["labels"], [("a", "1"), ("b", "2")])
        self.assertEqual(counters[0]["value"], 2)

        bad_label_sets = [
            (("k", 1), ("k", 2)),       # 重复键
            (({"x"}, 1),),              # set 不可 JSON 序列化
            (("v", object()),),         # 任意对象不可序列化
            (("v", float("nan")),),     # NaN 不可序列化（allow_nan=False）
        ]
        for bad in bad_label_sets:
            with self.assertRaises(ValueError):
                t.inc("bad", labels=bad)
            with self.assertRaises(ValueError):
                t.observe("bad", 1.0, labels=bad)
        # 拒绝不改变已有聚合
        self.assertEqual(len(t.snapshot()["counters"]), 1)
        self.assertEqual([s["name"] for s in t.snapshot()["samples"]], [])

    def test_sample_stats(self):
        t = Telemetry()
        t.observe("lat", 1, service="api")
        t.observe("lat", 2.5, service="api")
        t.observe("lat", "3.5", service="api")
        s = [x for x in t.snapshot()["samples"] if x["name"] == "lat"][0]
        self.assertEqual(s["values"], [1, 2.5, "3.5"])  # 原值与写入顺序保留
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["sum"], 7.0)
        self.assertEqual(s["minimum"], 1.0)
        self.assertEqual(s["maximum"], 3.5)
        self.assertEqual(s["mean"], 7.0 / 3)
        for bad in (float("nan"), float("inf"), float("-inf"), "nope", None):
            with self.assertRaises(ValueError):
                t.observe("lat", bad)
        self.assertEqual(
            [x for x in t.snapshot()["samples"] if x["name"] == "lat"][0]["values"],
            [1, 2.5, "3.5"],
        )
        # 不因重新读取而改变
        self.assertEqual(t.json(), t.json())

    def test_empty_sample_has_no_stats(self):
        t = Telemetry()
        t.samples[("", "e", ())] = []
        entry = t.snapshot()["samples"][0]
        self.assertEqual(set(entry), {"service", "name", "labels", "values"})

    def test_span_query(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("r1")
        t.finish("r1", error="boom")
        t.start("r1", service="api")  # open
        t.start("r2", service="api", parent="r1")
        t.finish("r2", service="api")  # ended, no error

        opened = t.query("open")
        errored = t.query("error")
        closed = t.query("closed")
        self.assertEqual([(e["service"], e["span"]) for e in opened], [("api", "r1")])
        self.assertEqual([(e["service"], e["span"]) for e in errored], [("", "r1")])
        # closed：所有 end 已写入的跨度，成功结束与带异常结束都包含，
        # 仍未结束的 api/r1 不出现；沿用 service、start、span 顺序。
        self.assertEqual(
            [(e["service"], e["span"]) for e in closed],
            [("", "r1"), ("api", "r2")],
        )
        self.assertEqual(
            set(closed[0]),
            {"span", "service", "parent", "start", "end", "error"},
        )
        self.assertIsNone(closed[1]["error"])  # 成功结束的跨度同样属于 closed
        self.assertIsNone(errored[0]["parent"])
        self.assertEqual(errored[0]["error"], "boom")
        self.assertIsNotNone(errored[0]["end"])
        for bad in ("", "CLOSED", "done", None, 0, b"closed", ["closed"]):
            with self.assertRaises(ValueError):
                t.query(bad)
        self.assertEqual(t.query("error"), t.query("error"))

    def test_span_service_locator_and_parent(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("same", parent="p-default")
        t.start("same", parent="p-api", service="api")
        t.finish("same", service="api", error=ValueError("x"))
        default_open = [e for e in t.query("open") if e["span"] == "same"]
        self.assertEqual(len(default_open), 1)
        self.assertEqual(default_open[0]["service"], "")
        self.assertEqual(default_open[0]["parent"], "p-default")
        errored = t.query("error")
        self.assertEqual(errored[0]["service"], "api")
        self.assertEqual(errored[0]["parent"], "p-api")
        self.assertIsInstance(errored[0]["error"], ValueError)

    def test_snapshot_ordering_and_json(self):
        t = Telemetry(iter(range(100)).__next__)
        t.inc("z", service="b")
        t.inc("a", service="a")
        t.observe("m", 1.0, service="a")
        t.start("z", service="b")
        t.start("a", service="b")
        t.start("m", service="a")
        snap = t.snapshot()
        self.assertEqual(
            [(c["service"], c["name"]) for c in snap["counters"]],
            [("a", "a"), ("b", "z")],
        )
        self.assertEqual(
            [(x["service"], x["start"], x["span"]) for x in snap["spans"]],
            [("a", 2, "m"), ("b", 0, "z"), ("b", 1, "a")],
        )
        self.assertEqual(
            t.json(),
            json.dumps(snap, sort_keys=True, separators=(",", ":")),
        )
        self.assertNotIn(": ", t.json())
        self.assertNotIn(", ", t.json())

    def test_no_network_modules(self):
        import inspect
        src = inspect.getsource(appmod)
        for forbidden in ("socket", "urllib", "requests", "http.client"):
            self.assertNotIn(forbidden, src)


class TelemetryRestoreTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(100)).__next__)
        t.inc("hits", 2, labels=(("b", "2"), ("a", "1")))
        t.inc("hits", service="api")
        t.observe("lat", 1, service="api")
        t.observe("lat", 2.5, service="api")
        t.observe("lat", "3.5", service="api")
        t.start("r1")
        t.finish("r1", error="boom")
        t.start("r2", service="api", parent="r1")
        return t

    def test_roundtrip_via_snapshot_object(self):
        t = self.build()
        restored = Telemetry.from_snapshot(t.snapshot())
        self.assertEqual(restored.snapshot(), t.snapshot())
        self.assertEqual(restored.json(), t.json())
        self.assertEqual(restored.query("open"), t.query("open"))
        self.assertEqual(restored.query("error"), t.query("error"))

    def test_roundtrip_via_json_text(self):
        t = self.build()
        restored = Telemetry.from_snapshot(t.json())
        self.assertEqual(restored.json(), t.json())
        self.assertEqual(restored.snapshot(), t.snapshot())

    def test_restored_instance_is_independent(self):
        t = self.build()
        snap = t.snapshot()
        restored = Telemetry.from_snapshot(snap)
        # 修改原快照对象不影响已恢复的实例
        snap["counters"][0]["value"] = 999
        snap["samples"][0]["values"].append(100)
        snap["spans"][0]["parent"] = "changed"
        self.assertNotEqual(restored.snapshot()["counters"][0]["value"], 999)
        self.assertEqual(len(restored.snapshot()["samples"][0]["values"]), 3)
        self.assertIsNone(restored.snapshot()["spans"][0]["parent"])
        # 继续写入新实例不影响原实例
        restored.inc("hits")
        self.assertEqual(
            [c["value"] for c in t.snapshot()["counters"] if c["service"] == ""],
            [2],
        )

    def test_finish_open_span_after_restore(self):
        t = self.build()
        restored = Telemetry.from_snapshot(t.json(), clock=lambda: 42.0)
        self.assertEqual([e["span"] for e in restored.query("open")], ["r2"])
        restored.finish("r2", service="api", error="late")
        self.assertEqual(restored.query("open"), [])
        errored = restored.query("error")
        self.assertEqual(
            [(e["service"], e["span"]) for e in errored],
            [("", "r1"), ("api", "r2")],
        )
        self.assertEqual(errored[1]["end"], 42.0)

    def test_non_strict_error_only_via_json(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("s")
        t.finish("s", error=ValueError("x"))
        # snapshot() 对象中的异常实例不可严格 JSON 表示，不能直接恢复
        with self.assertRaises(ValueError):
            Telemetry.from_snapshot(t.snapshot())
        restored = Telemetry.from_snapshot(t.json())
        self.assertEqual(
            restored.query("error")[0]["error"],
            {"type": "ValueError", "message": "x"},
        )

    def test_stats_recomputed_and_checked(self):
        base = {"counters": [], "spans": []}
        # 缺失统计字段：按 values 重算补齐
        payload = dict(base, samples=[
            {"service": "", "name": "m", "labels": [], "values": [1, 2.5]},
        ])
        entry = Telemetry.from_snapshot(payload).snapshot()["samples"][0]
        self.assertEqual(entry["count"], 2)
        self.assertEqual(entry["sum"], 3.5)
        self.assertEqual(entry["mean"], 3.5 / 2)
        # 一致统计可通过
        good = Telemetry().snapshot()
        good["samples"] = [{
            "service": "", "name": "m", "labels": [], "values": [1, 2.5],
            "count": 2, "sum": 3.5, "minimum": 1.0, "maximum": 2.5, "mean": 1.75,
        }]
        self.assertEqual(Telemetry.from_snapshot(good).snapshot(), good)
        # 不一致统计拒绝
        for bad_stats in ({"count": 3}, {"sum": 9.9}, {"mean": 0.0}, {"maximum": 1}):
            bad = Telemetry().snapshot()
            bad["samples"] = [dict(
                {"service": "", "name": "m", "labels": [], "values": [1, 2.5]},
                **bad_stats,
            )]
            with self.assertRaises(ValueError):
                Telemetry.from_snapshot(bad)
        # 空 values 不得携带统计
        bad = Telemetry().snapshot()
        bad["samples"] = [
            {"service": "", "name": "m", "labels": [], "values": [], "count": 0},
        ]
        with self.assertRaises(ValueError):
            Telemetry.from_snapshot(bad)

    def test_invalid_payloads_raise_valueerror(self):
        t = self.build()
        empty = {"counters": [], "samples": [], "spans": []}
        span = {"span": "s", "service": "", "parent": None,
                "start": 0, "end": None, "error": None}
        counter = {"service": "", "name": "x", "labels": [], "value": 1}
        sample = {"service": "", "name": "m", "labels": [], "values": [1.0]}
        cases = [
            "{not json}",
            "[1, 2]",
            42,
            json.dumps({"counters": [], "samples": []}),          # 缺 spans
            json.dumps(dict(empty, extra=[])),                    # 多余顶层键
            '{"counters":[],"counters":[],"samples":[],"spans":[]}',  # 重复键
            '{"counters":[],"samples":[],"spans":[],"x":NaN}',    # 非严格常量
            dict(empty, counters={}),                             # 非数组
            dict(empty, counters=[dict(counter, value=None)]),    # value 缺类型? None 可表示 -> 合法，见下
            dict(empty, counters=[{k: v for k, v in counter.items() if k != "value"}]),
            dict(empty, counters=[dict(counter, z=1)]),           # 多余字段
            dict(empty, counters=[counter, dict(counter)]),       # 重复记录
            dict(empty, counters=[dict(counter, labels=[["k", 1], ["k", 2]])]),
            dict(empty, counters=[dict(counter, labels=[["k", float("nan")]])]),
            dict(empty, counters=[dict(counter, service=1)]),
            dict(empty, samples=[dict(sample, values="nope")]),
            dict(empty, samples=[dict(sample, values=[float("inf")])]),
            dict(empty, samples=[dict(sample, values=["nan"])]),
            dict(empty, samples=[dict(sample, unknown=1)]),
            dict(empty, samples=[sample, dict(sample)]),          # 重复样本
            dict(empty, spans=[dict(span, end="2020")]),          # 合法：已结束值
            dict(empty, spans=[{k: v for k, v in span.items() if k != "error"}]),
            dict(empty, spans=[dict(span), dict(span)]),          # 重复跨度
            dict(empty, spans=[dict(span, span=[1])]),            # 不可哈希标识
            dict(empty, spans=[dict(span, error=object())]),      # 不可表示 error
            dict(empty, spans=[dict(span, start=float("nan"))]),
        ]
        legal = {8, 20}  # None 计数、字符串 end 等按规则合法的用例索引
        for index, payload in enumerate(cases):
            if index in legal:
                Telemetry.from_snapshot(payload)
            else:
                with self.assertRaises(ValueError, msg="case %d" % index):
                    Telemetry.from_snapshot(payload)
        # 失败恢复不影响已有实例
        self.assertEqual(t.snapshot(), self.build().snapshot())


class TelemetryMergeTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.inc("hits", 2, labels=(("a", 1),))
        t.inc("only", 5, service="api")
        t.observe("lat", 1, service="api")
        t.observe("lat", 2.5, service="api")
        t.start("r1")
        t.finish("r1", error="boom")
        t.start("open1", service="api", parent="p")
        return t

    def shard(self):
        p = Telemetry(iter(range(2000)).__next__)
        p.inc("hits", 3, labels=(("a", 1),))
        p.inc("newhits", 7)
        p.observe("lat", "3.5", service="api")
        p.observe("lat", 4, service="web")
        p.samples[("", "empty", ())] = []
        p.start("open2", service="api", parent=("x",))
        p.start("r1")
        p.finish("r1", error="boom")
        p.start("onlyspan", service="web")
        p.finish("onlyspan", service="web", error={"code": 1})
        snap = p.snapshot()
        # r1 与当前实例完全一致（start 0/end 1/error boom）
        for record in snap["spans"]:
            if record["span"] == "r1":
                record["start"] = 0
                record["end"] = 1
        return snap

    def test_accepts_dict_text_and_bytes(self):
        text = json.dumps(self.shard(), sort_keys=True, separators=(",", ":"))
        for raw in (self.shard(), text, text.encode("utf-8")):
            t = self.build()
            self.assertIsNone(t.merge_snapshot(raw))
            counters = {
                (c["service"], c["name"], tuple(c["labels"])): c["value"]
                for c in t.snapshot()["counters"]
            }
            self.assertEqual(counters[("", "hits", (("a", 1),))], 5)
            self.assertEqual(counters[("api", "only", ())], 5)
            self.assertEqual(counters[("", "newhits", ())], 7)

    def test_samples_concatenated_and_stats_recomputed(self):
        t = self.build()
        t.merge_snapshot(self.shard())
        samples = {(s["service"], s["name"]): s for s in t.snapshot()["samples"]}
        self.assertEqual(samples[("api", "lat")]["values"], [1, 2.5, "3.5"])
        self.assertEqual(samples[("api", "lat")]["count"], 3)
        self.assertEqual(samples[("api", "lat")]["sum"], 7.0)
        self.assertEqual(samples[("api", "lat")]["mean"], 7.0 / 3)
        self.assertEqual(samples[("web", "lat")]["values"], [4])
        self.assertEqual(
            set(samples[("", "empty")]),
            {"service", "name", "labels", "values"},
        )

    def test_spans_query_finish_and_parent(self):
        t = self.build()
        t.merge_snapshot(self.shard())
        self.assertEqual(
            {(e["service"], e["span"]) for e in t.query("open")},
            {("api", "open1"), ("api", "open2")},
        )
        self.assertEqual(
            {(e["service"], e["span"]) for e in t.query("error")},
            {("", "r1"), ("web", "onlyspan")},
        )
        t.clock = lambda: 999.0
        t.finish("open2", service="api", error="late")
        entry = [e for e in t.query("error") if e["span"] == "open2"][0]
        self.assertEqual(entry["end"], 999.0)
        self.assertEqual(entry["parent"], ("x",))

    def test_identical_span_is_idempotent_conflict_rolls_back(self):
        t = self.build()
        before = t.snapshot()
        spans_only = {"counters": [], "samples": [], "spans": before["spans"]}
        t.merge_snapshot(spans_only)
        self.assertEqual(t.snapshot(), before)

        conflict = self.shard()
        for record in conflict["spans"]:
            if record["span"] == "r1":
                record["error"] = "different"
        with self.assertRaises(ValueError):
            t.merge_snapshot(conflict)
        self.assertEqual(t.snapshot(), before)  # 计数器累加等变化一并撤销

    def test_label_normalization_keys_merge(self):
        a = Telemetry()
        a.inc("m", 1, labels=(("a", 1), ("b", 2)))
        b = Telemetry()
        b.inc("m", 4, labels=(("b", 2), ("a", 1)))
        a.merge_snapshot(b.snapshot())
        snap = a.snapshot()["counters"]
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["labels"], [("a", 1), ("b", 2)])
        self.assertEqual(snap[0]["value"], 5)

    def test_counter_add_failure_rolls_back(self):
        t = Telemetry.from_snapshot({
            "counters": [
                {"service": "", "name": "c", "labels": [], "value": "ab"},
            ],
            "samples": [],
            "spans": [],
        })
        before = t.snapshot()
        with self.assertRaises(ValueError):
            t.merge_snapshot({
                "counters": [
                    {"service": "", "name": "c", "labels": [], "value": 1},
                ],
                "samples": [],
                "spans": [],
            })
        self.assertEqual(t.snapshot(), before)

    def test_input_not_mutated_and_not_shared(self):
        t = self.build()
        payload = self.shard()
        payload_copy = copy.deepcopy(payload)
        t.merge_snapshot(payload)
        self.assertEqual(payload, payload_copy)  # 输入未被改写
        # 改动 payload 内部对象不影响已合并的实例
        payload["counters"][0]["value"] = 999
        payload["samples"][0]["values"].append(999)
        payload["spans"][0]["parent"] = "changed"
        again = self.build()
        again.merge_snapshot(payload_copy)
        self.assertEqual(t.snapshot(), again.snapshot())

    def test_invalid_payloads_leave_instance_untouched(self):
        t = self.build()
        before = t.snapshot()
        good_span = {"span": "s", "service": "", "parent": None,
                     "start": 0, "end": None, "error": None}
        cases = [
            b"\xff not utf-8",
            "{not json",
            {"counters": [], "samples": []},
            {"counters": [], "samples": [], "spans": [], "extra": 1},
            {"counters": [
                {"service": "", "name": "c", "labels": [], "value": 1},
                {"service": "", "name": "c", "labels": [], "value": 2},
            ], "samples": [], "spans": []},
            {"counters": [], "samples": [], "spans": [dict(good_span), dict(good_span)]},
            {"counters": [],
             "samples": [{"service": "", "name": "m", "labels": [],
                          "values": [float("nan")]}],
             "spans": []},
            {"counters": [{"service": "", "name": "c",
                           "labels": [["k", float("nan")]], "value": 1}],
             "samples": [], "spans": []},
        ]
        for index, payload in enumerate(cases):
            with self.assertRaises(ValueError, msg="case %d" % index):
                t.merge_snapshot(payload)
            self.assertEqual(t.snapshot(), before, msg="case %d" % index)


class TelemetryTraceTest(unittest.TestCase):
    def build(self):
        clock = iter(range(100)).__next__
        t = Telemetry(clock)
        t.start("root")                       # 0
        t.start("b", parent="root")           # 1
        t.start("a", parent="root")           # 2
        t.start("a1", parent="a")             # 3
        t.finish("a1", error="boom")          # 4
        t.start("root", service="api")        # 5，同名但不同服务
        t.start("x", service="api", parent="root")  # 6，不属于默认服务的树
        t.start("orphan", parent="ghost")     # 7，父标识不存在
        return t

    def test_trace_builds_sorted_tree(self):
        t = self.build()
        tree = t.trace("root")
        self.assertEqual(
            set(tree),
            {"span", "service", "parent", "start", "end", "error", "children"},
        )
        self.assertEqual(tree["span"], "root")
        self.assertEqual(tree["service"], "")
        self.assertIsNone(tree["parent"])
        # children 按服务、开始时间、标识排序：b(1) 在 a(2) 前
        self.assertEqual([c["span"] for c in tree["children"]], ["b", "a"])
        self.assertEqual(tree["children"][0]["children"], [])
        a = tree["children"][1]
        self.assertEqual([c["span"] for c in a["children"]], ["a1"])
        self.assertEqual(a["children"][0]["error"], "boom")
        self.assertIsNotNone(a["children"][0]["end"])
        self.assertEqual(a["children"][0]["children"], [])

    def test_trace_service_isolation(self):
        t = self.build()
        tree = t.trace("root", service="api")
        self.assertEqual(tree["service"], "api")
        # 只含同服务的直接子跨度；默认服务的 b/a 不出现
        self.assertEqual([c["span"] for c in tree["children"]], ["x"])
        # 父标识指向不存在的跨度：orphan 不是任何人的子节点
        self.assertNotIn("orphan", json.dumps(t.trace("root"), default=str))

    def test_trace_missing_root_returns_none(self):
        t = self.build()
        self.assertIsNone(t.trace("nope"))
        self.assertIsNone(t.trace("root", service="web"))  # 服务不同即不存在

    def test_trace_invalid_arguments(self):
        t = self.build()
        for bad in ("", 1, b"x"):
            with self.assertRaises(ValueError):
                t.trace("root", service=bad)
        for bad in (["root"], {"s": 1}):  # 不可哈希的跨度标识
            with self.assertRaises(ValueError):
                t.trace(bad)

    def test_trace_cycle_raises_without_partial_tree(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("a", parent="b")
        t.start("b", parent="a")
        with self.assertRaises(ValueError):
            t.trace("a")
        t2 = Telemetry(iter(range(100)).__next__)
        t2.start("self", parent="self")  # 自环
        with self.assertRaises(ValueError):
            t2.trace("self")

    def test_trace_result_is_independent(self):
        t = self.build()
        before = t.snapshot()
        tree = t.trace("root")
        tree["children"][0]["span"] = "mutated"
        tree["children"].clear()
        tree["span"] = "mutated"
        self.assertEqual(t.snapshot(), before)  # 聚合器不受影响
        again = t.trace("root")
        self.assertEqual([c["span"] for c in again["children"]], ["b", "a"])

    def test_trace_does_not_mutate_and_matches_snapshot_fields(self):
        t = self.build()
        before = t.snapshot()
        tree = t.trace("root")
        self.assertEqual(t.snapshot(), before)  # 查询本身无副作用
        entry = [e for e in before["spans"]
                 if e["service"] == "" and e["span"] == "a1"][0]
        node = tree["children"][1]["children"][0]
        for field in ("span", "service", "parent", "start", "end", "error"):
            self.assertEqual(node[field], entry[field])


class TelemetryBatchTest(unittest.TestCase):
    def _events(self):
        return [
            {"op": "inc", "name": "requests", "labels": (("a", 1),)},
            {"op": "inc", "name": "requests", "value": 2, "service": "api"},
            {"op": "observe", "name": "lat", "value": 1, "service": "api"},
            {"op": "observe", "name": "lat", "value": "2.5", "service": "api"},
            {"op": "start", "span": "root"},
            {"op": "start", "span": "child", "parent": "root", "service": "api"},
            {"op": "finish", "span": "root", "error": "boom"},
            {"op": "finish", "span": "child", "service": "api"},
        ]

    def _sequential(self, events, clock_factory):
        t = Telemetry(clock_factory())
        for event in events:
            kwargs = {k: v for k, v in event.items() if k != "op"}
            getattr(t, event["op"])(**kwargs)
        return t

    def test_matches_sequential_calls(self):
        events = self._events()
        seq = self._sequential(copy.deepcopy(events),
                               lambda: iter(range(1000)).__next__)
        bat = Telemetry(iter(range(1000)).__next__)
        self.assertIsNone(bat.batch(copy.deepcopy(events)))
        self.assertEqual(bat.snapshot(), seq.snapshot())
        self.assertEqual(bat.json(), seq.json())
        for status in ("open", "closed", "error"):
            self.assertEqual(bat.query(status), seq.query(status))
        self.assertEqual(bat.trace("root"), seq.trace("root"))
        self.assertEqual(bat.trace("child", service="api"),
                         seq.trace("child", service="api"))

    def test_tuple_accepted_and_later_spans_usable(self):
        t = Telemetry(iter(range(100)).__next__)
        self.assertIsNone(t.batch((
            {"op": "start", "span": "a"},
            {"op": "start", "span": "b", "parent": "a"},
            {"op": "finish", "span": "b"},
            {"op": "finish", "span": "a"},
        )))
        self.assertEqual(t.query("open"), [])

    def test_empty_batch_is_success_and_does_not_read_clock(self):
        reads = []
        t = Telemetry(lambda: reads.append(1) or 0.0)
        t.inc("x")
        before = t.snapshot()
        self.assertIsNone(t.batch([]))
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(reads, [])

    def test_clock_read_once_per_start_and_finish_in_order(self):
        t = Telemetry(iter(range(100)).__next__)
        t.batch([
            {"op": "start", "span": "a"},
            {"op": "start", "span": "b"},
            {"op": "finish", "span": "a"},
        ])
        a = t.spans[("", "a")]
        b = t.spans[("", "b")]
        self.assertEqual((a["start"], b["start"], a["end"]), (0, 1, 2))
        self.assertIsNone(b["end"])

    def test_invalid_events_raise_valueerror(self):
        cases = [
            None, 42, "nope", {"op": "inc"}, {1: 2},
            [{"name": "x"}],                            # 缺 op
            [{"op": "INC", "name": "x"}],               # 未知 op
            [{"op": ["inc"]}],                          # op 不可哈希
            [{"op": "inc", "name": "x", "extra": 1}],   # 未知字段
            [{"op": "observe", "value": 1}],            # 缺 name
            [{"op": "observe", "name": "m"}],           # observe 缺 value
            [{"op": "start"}],                          # 缺 span
            [{"op": "finish"}],                         # 缺 span
            [{"op": "start", "span": "s", "value": 1}],
            [{"op": "inc", "name": "x", "parent": None}],
            ["not-an-object"],
            [{"op": "inc", "name": "x", "service": ""}],
            [{"op": "inc", "name": "x",
              "labels": (("k", 1), ("k", 2))}],
            [{"op": "observe", "name": "m", "value": float("nan")}],
            [{"op": "observe", "name": "m", "value": "nope"}],
            [{"op": "start", "span": ["unhashable"]}],
            [{"op": "finish", "span": ["unhashable"]}],
        ]
        for events in cases:
            with self.assertRaises(ValueError, msg=repr(events)):
                Telemetry().batch(events)

    def test_span_lifecycle_violations_rejected(self):
        existing = Telemetry(iter(range(10)).__next__)
        existing.start("done")
        existing.finish("done")
        cases = [
            [{"op": "start", "span": "a"},
             {"op": "start", "span": "a"}],                 # 重复开始
            [{"op": "start", "span": "done"}],              # 已结束标识
            [{"op": "finish", "span": "ghost"}],            # 不存在
            [{"op": "start", "span": "a"},
             {"op": "finish", "span": "a"},
             {"op": "finish", "span": "a"}],                # 二次结束
        ]
        for events in cases:
            t = Telemetry(iter(range(1000)).__next__)
            t.counters = dict(existing.counters)
            t.spans = {k: dict(v) for k, v in existing.spans.items()}
            before = t.snapshot()
            with self.assertRaises(ValueError, msg=repr(events)):
                t.batch(events)
            self.assertEqual(t.snapshot(), before, msg=repr(events))

    def test_rejection_is_atomic_and_does_not_read_clock(self):
        t = Telemetry(iter(range(100)).__next__)
        t.inc("before")
        before = t.snapshot()

        class AssertingClock:
            reads = 0
            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read on rejection")

        t.clock = AssertingClock()
        events = [
            {"op": "inc", "name": "inbatch"},
            {"op": "observe", "name": "m", "value": 3.0},
            {"op": "start", "span": "s"},
            {"op": "finish", "span": "s"},
            {"op": "start", "span": "s"},  # 重复开始，整批拒绝
        ]
        with self.assertRaises(ValueError):
            t.batch(events)
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.query("open"), [])
        self.assertEqual(AssertingClock.reads, 0)

    def test_clock_exception_propagates_and_rolls_back(self):
        class ClockError(RuntimeError):
            pass

        state = {"n": 0}

        def clock():
            state["n"] += 1
            if state["n"] == 2:  # 第二个 start 的 clock 失败
                raise ClockError("clock broke")
            return float(state["n"])

        t = Telemetry(clock)
        t.inc("before")
        before = t.snapshot()
        with self.assertRaises(ClockError):
            t.batch([
                {"op": "start", "span": "a"},
                {"op": "start", "span": "b"},
            ])
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.query("open"), [])

    def test_input_events_not_modified(self):
        labels = [("b", 2), ("a", 1)]
        events = [
            {"op": "inc", "name": "c", "labels": labels, "value": 5},
            {"op": "start", "span": "r", "parent": ("p",)},
        ]
        saved = copy.deepcopy(events)
        t = Telemetry(iter(range(10)).__next__)
        t.batch(events)
        self.assertEqual(events, saved)
        self.assertEqual(labels, [("b", 2), ("a", 1)])
        # 快照中标签已归一化；事后修改输入不影响聚合器
        labels.append(("z", 9))
        self.assertEqual(
            t.snapshot()["counters"][0]["labels"], [("a", 1), ("b", 2)]
        )

    def test_no_new_snapshot_fields_and_coexists_with_restore_merge(self):
        t = Telemetry(iter(range(100)).__next__)
        t.batch([
            {"op": "inc", "name": "h", "value": 2},
            {"op": "observe", "name": "m", "value": 1.5},
            {"op": "start", "span": "r"},
            {"op": "finish", "span": "r", "error": 0},
        ])
        self.assertEqual(set(t.snapshot()), {"counters", "samples", "spans"})
        # error=0（假值非 None）算异常，与逐条语义一致
        self.assertEqual([e["span"] for e in t.query("error")], ["r"])
        restored = Telemetry.from_snapshot(t.json())
        self.assertEqual(restored.snapshot(), t.snapshot())
        other = Telemetry()
        self.assertIsNone(other.merge_snapshot(t.snapshot()))
        self.assertEqual(other.snapshot(), t.snapshot())


class TelemetrySnapshotFilterTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(100)).__next__)
        t.inc("hits")
        t.inc("hits", 2, labels=(("b", 2), ("a", 1)))
        t.inc("hits", service="api")
        t.inc("hits", 5, labels=(("k", "v"),), service="api")
        t.observe("lat", 1)
        t.observe("lat", 3, service="api")
        t.observe("lat", 2, labels=(("k", "v"),), service="api")
        t.start("s1")
        t.finish("s1", error="boom")                 # 默认服务 closed+error
        t.start("s2", service="api")                 # api open
        t.start("s3", service="api", parent="s2")
        t.finish("s3", service="api")                # api closed 成功
        t.start("s4", parent="s1")
        t.finish("s4", error=0)                      # error=0 也算异常
        return t

    def test_omitted_filters_identical_to_no_args(self):
        t = self.build()
        base = t.snapshot()
        self.assertEqual(t.snapshot(None, None, None), base)
        self.assertEqual(t.snapshot(service=None, labels=None, status=None), base)
        self.assertEqual(t.json(None, None, None), t.json())
        self.assertEqual(set(base), {"counters", "samples", "spans"})
        # 字段集合仍是既有字段
        for record in base["counters"]:
            self.assertEqual(set(record), {"service", "name", "labels", "value"})
        for record in base["samples"]:
            self.assertTrue(
                {"service", "name", "labels", "values"} <= set(record)
            )
        for record in base["spans"]:
            self.assertEqual(
                set(record),
                {"span", "service", "parent", "start", "end", "error"},
            )

    def test_service_filter_applies_to_all_arrays(self):
        t = self.build()
        api = t.snapshot(service="api")
        self.assertEqual({c["service"] for c in api["counters"]}, {"api"})
        self.assertEqual({s["service"] for s in api["samples"]}, {"api"})
        self.assertEqual({x["service"] for x in api["spans"]}, {"api"})
        self.assertEqual(
            [(c["name"], c["value"]) for c in api["counters"]],
            [("hits", 1), ("hits", 5)],
        )
        self.assertEqual(
            [x["span"] for x in api["spans"]], ["s2", "s3"]
        )
        # 空字符串表示默认服务
        dflt = t.snapshot(service="")
        self.assertEqual({c["service"] for c in dflt["counters"]}, {""})
        self.assertEqual([x["span"] for x in dflt["spans"]], ["s1", "s4"])

    def test_labels_exact_normalized_match_for_counters_and_samples(self):
        t = self.build()
        lab = t.snapshot(labels=(("a", 1), ("b", 2)))
        self.assertEqual(
            [(c["service"], c["value"]) for c in lab["counters"]], [("", 2)]
        )
        self.assertEqual(lab["counters"][0]["labels"], [("a", 1), ("b", 2)])
        self.assertEqual(lab["samples"], [])
        # 输入顺序不同、归一化后相同 -> 精确命中同一集合
        self.assertEqual(t.snapshot(labels=(("b", 2), ("a", 1))), lab)
        # 标签筛选不限制跨度
        self.assertEqual(
            t.snapshot(labels=(("a", 1), ("b", 2)))["spans"],
            t.snapshot()["spans"],
        )
        # 显式空标签只命中无标签记录
        no_lab = t.snapshot(labels=())
        self.assertTrue(all(c["labels"] == [] for c in no_lab["counters"]))
        self.assertEqual(
            {(c["service"], c["value"]) for c in no_lab["counters"]},
            {("", 1), ("api", 1)},
        )
        # 样本同样按完整标签集合匹配
        hit = t.snapshot(service="api", labels=(("k", "v"),))
        self.assertEqual(hit["samples"][0]["values"], [2])
        # 子集不算命中
        t2 = Telemetry()
        t2.inc("n", labels=(("a", 1), ("b", 2)))
        self.assertEqual(t2.snapshot(labels=(("a", 1),))["counters"], [])

    def test_status_matches_query_definitions_and_order(self):
        t = self.build()
        for status in ("open", "closed", "error"):
            self.assertEqual(
                t.snapshot(status=status)["spans"], t.query(status)
            )
        self.assertEqual(
            [x["span"] for x in t.snapshot(status="closed")["spans"]],
            ["s1", "s4", "s3"],
        )
        # status 只作用于跨度，计数器/样本不受影响
        base = t.snapshot()
        self.assertEqual(t.snapshot(status="open")["counters"], base["counters"])
        self.assertEqual(t.snapshot(status="error")["samples"], base["samples"])

    def test_service_and_status_combine_parent_unchanged(self):
        t = self.build()
        self.assertEqual(
            [x["span"] for x in t.snapshot(service="api", status="open")["spans"]],
            ["s2"],
        )
        closed = t.snapshot(service="api", status="closed")["spans"]
        self.assertEqual([x["span"] for x in closed], ["s3"])
        self.assertEqual(closed[0]["parent"], "s2")  # 父标识不被筛选改写
        self.assertEqual(
            t.snapshot(service="api", status="error")["spans"], []
        )

    def test_stats_recomputed_per_matching_series(self):
        t = Telemetry()
        t.observe("m", 1, service="a")
        t.observe("m", 3, service="a")
        t.observe("m", 100, service="b")
        entry = t.snapshot(service="a")["samples"][0]
        self.assertEqual(entry["count"], 2)
        self.assertEqual(entry["sum"], 4.0)
        self.assertEqual(entry["mean"], 2.0)
        self.assertEqual(entry["minimum"], 1.0)
        self.assertEqual(entry["maximum"], 3.0)
        # 空样本序列命中时仍不产生统计
        t.samples[("", "empty", ())] = []
        empty = t.snapshot(service="")["samples"]
        self.assertTrue(
            all(set(x) == {"service", "name", "labels", "values"} for x in empty)
        )

    def test_no_match_means_empty_arrays(self):
        t = self.build()
        self.assertEqual(
            t.snapshot(service="nope"),
            {"counters": [], "samples": [], "spans": []},
        )
        self.assertEqual(t.snapshot(labels=(("z", 1),))["counters"], [])
        self.assertEqual(t.snapshot(labels=(("z", 1),))["samples"], [])
        self.assertEqual(
            t.snapshot(service="api", status="error")["spans"], []
        )

    def test_invalid_filters_raise_before_reading_and_without_clock(self):
        t = self.build()
        before = t.snapshot()

        class AssertingClock:
            reads = 0

            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read by snapshot/json")

        t.clock = AssertingClock()
        cases = [
            {"service": 1},
            {"service": b"api"},
            {"service": ["api"]},
            {"service": True},
            {"labels": (("k", 1), ("k", 2))},
            {"labels": (("v", object()),)},
            {"labels": (("v", float("nan")),)},
            {"labels": 42},
            {"status": ""},
            {"status": "CLOSED"},
            {"status": "done"},
            {"status": 0},
            {"service": 1, "status": "open"},
            {"labels": (("k", 1),), "status": "bad"},
        ]
        for kwargs in cases:
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                t.snapshot(**kwargs)
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                t.json(**kwargs)
        self.assertEqual(AssertingClock.reads, 0)
        t.clock = iter(range(100)).__next__
        self.assertEqual(t.snapshot(), before)  # 无部分结果、内部数据未变

    def test_results_independent_and_repeatable(self):
        t = self.build()
        before = t.snapshot()
        first = t.snapshot(service="api", labels=(), status="closed")
        second = t.snapshot(service="api", labels=(), status="closed")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["counters"].append("x")
        first["samples"] = []
        first["spans"][0]["span"] = "mutated"
        self.assertEqual(
            t.snapshot(service="api", labels=(), status="closed"), second
        )
        # 无筛选结果同样独立：改写返回对象不影响聚合器
        snap = t.snapshot()
        snap["counters"][0]["labels"].append(("zzz", 0))
        snap["samples"][0]["values"].append(999)
        snap["spans"][0]["error"] = "changed"
        self.assertEqual(t.snapshot(), before)
        # 标签输入对象不被修改或共享
        labels = [("b", 2), ("a", 1)]
        result = t.snapshot(labels=labels)
        self.assertEqual(labels, [("b", 2), ("a", 1)])
        labels.append(("c", 3))
        self.assertEqual(t.snapshot(labels=[("b", 2), ("a", 1)]), result)
        # 内部存储顺序不被筛选改变
        self.assertEqual(t.snapshot(), before)

    def test_json_filter_matches_snapshot_compact_and_error_rule(self):
        t = self.build()
        for kwargs in (
            {},
            {"service": "api"},
            {"labels": ()},
            {"status": "closed"},
            {"service": "api", "labels": (("k", "v"),), "status": "open"},
        ):
            self.assertEqual(
                t.json(**kwargs),
                json.dumps(
                    t.snapshot(**kwargs),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        text = t.json(service="api")
        self.assertNotIn(": ", text)
        self.assertNotIn(", ", text)

        t2 = Telemetry(iter(range(10)).__next__)
        t2.start("e")
        t2.finish("e", error=ValueError("x"))
        t2.start("ok")
        t2.finish("ok")
        t2.start("o")
        self.assertEqual(
            json.loads(t2.json(status="error"))["spans"][0]["error"],
            {"type": "ValueError", "message": "x"},
        )
        self.assertIsNone(json.loads(t2.json())["spans"][1]["error"])
        self.assertEqual(
            json.loads(t2.json(status="open"))["spans"][0]["span"], "o"
        )


if __name__ == "__main__":
    unittest.main()
