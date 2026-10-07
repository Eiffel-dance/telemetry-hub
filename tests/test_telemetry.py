import copy
import json
import math
import unittest
from decimal import Decimal
from fractions import Fraction

import app as appmod
from app import SnapshotFormatError, Telemetry, TelemetryCapacityError


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

    def test_span_query_service_filter(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("d1")          # 默认服务，open
        t.start("d2")
        t.finish("d2")         # 默认服务，closed 无异常
        t.start("d3")
        t.finish("d3", error="bad")  # 默认服务，error
        t.start("a1", service="api")  # open
        t.start("a2", service="api")
        t.finish("a2", service="api")  # closed 无异常
        t.start("w1", service="web")
        t.finish("w1", service="web", error=0)  # 假值异常也算 error

        # 省略筛选与显式 None 都与既有 query(status) 逐项一致。
        for status in ("open", "closed", "error"):
            self.assertEqual(t.query(status), t.query(status, None))
            self.assertEqual(t.query(status), t.query(status, service=None))

        self.assertEqual(
            [(e["service"], e["span"]) for e in t.query("open", service="api")],
            [("api", "a1")],
        )
        self.assertEqual(
            [e["span"] for e in t.query("closed", service="api")],
            ["a2"],
        )
        # 服务内无匹配返回空列表；web 没有 open 跨度。
        self.assertEqual(t.query("open", service="web"), [])
        self.assertIsInstance(t.query("error", service="api"), list)
        # 空字符串表示默认服务，0 这类假值 error 仍按 error 定义命中。
        self.assertEqual(
            [(e["service"], e["span"]) for e in t.query("open", service="")],
            [("", "d1")],
        )
        self.assertEqual(
            [e["span"] for e in t.query("closed", service="")],
            ["d2", "d3"],
        )
        self.assertEqual(
            [(e["span"], e["error"]) for e in t.query("error", service="")],
            [("d3", "bad")],
        )
        self.assertEqual(
            [(e["span"], e["error"]) for e in t.query("error", service="web")],
            [("w1", 0)],
        )
        # 不存在的服务一律空列表，记录字段保持六个既有字段。
        self.assertEqual(t.query("closed", service="missing"), [])
        self.assertEqual(
            set(t.query("closed", service="api")[0]),
            {"span", "service", "parent", "start", "end", "error"},
        )
        # 带筛选结果与同口径 snapshot 完全一致（含排序与记录内容）。
        for status in ("open", "closed", "error"):
            for service in (None, "", "api", "web", "missing"):
                self.assertEqual(
                    t.query(status, service=service),
                    t.snapshot(status=status, service=service)["spans"],
                )

    def test_span_query_service_filter_validation_and_isolation(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("a", service="api")
        t.finish("a", service="api", error=["e"])
        before = t.snapshot()

        # service 只接受字符串或 None；status 非法时即使带服务筛选也拒绝。
        for bad in (0, 1, b"api", ["api"], ("api",), object()):
            with self.assertRaises(ValueError):
                t.query("open", bad)
            with self.assertRaises(ValueError):
                t.query("open", service=bad)
        for bad in ("OPEN", "", None, 0, [], object()):
            with self.assertRaises(ValueError):
                t.query(bad, service="api")
        # 任何拒绝都不改动聚合状态，且查询不读取 clock。
        self.assertEqual(t.snapshot(), before)

        def fail_clock():
            raise AssertionError("query must not read clock")

        t.clock = fail_clock
        for status in ("open", "closed", "error"):
            t.query(status, service="api")
        with self.assertRaises(ValueError):
            t.query("bad", service="api")
        with self.assertRaises(ValueError):
            t.query("open", service=1)

        # 返回记录与内部状态及同次其他记录互不共享。
        first = t.query("error", service="api")
        second = t.query("error", service="api")
        self.assertIsNot(first[0], second[0])
        first[0]["error"].append("mutated")
        self.assertEqual(t.query("error", service="api")[0]["error"], ["e"])
        first[0]["span"] = "hacked"
        self.assertEqual(t.query("error", service="api")[0]["span"], "a")

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


class TelemetryMergeManyTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.inc("hits", 2, labels=(("a", 1),))
        t.observe("lat", 1, service="api")
        t.start("r1")
        t.finish("r1", error="boom")
        return t

    def shard_a(self):
        p = Telemetry(iter(range(2000)).__next__)
        p.inc("hits", 3, labels=(("a", 1),))
        p.inc("only", 5, service="api")
        p.observe("lat", 2.5, service="api")
        p.start("open1", service="api", parent="p")
        return p.snapshot()

    def shard_b(self):
        p = Telemetry(iter(range(3000)).__next__)
        p.inc("hits", 4, labels=(("a", 1),))
        p.observe("lat", "3.5", service="api")
        p.observe("lat", 4, service="web")
        p.start("r2")
        p.finish("r2")
        return p.snapshot()

    def test_empty_collection_succeeds_without_touching_state(self):
        t = self.build()
        before = t.snapshot()
        for empty in ([], ()):
            self.assertIsNone(t.merge_snapshots(empty))
            self.assertEqual(t.snapshot(), before)

    def test_outer_container_must_be_list_or_tuple(self):
        t = self.build()
        before = t.snapshot()
        for bad in (None, "x", b"x", 1, {"counters": [], "samples": [], "spans": []},
                    {"a", "b"}, (s for s in ())):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.merge_snapshots(bad)
            self.assertEqual(t.snapshot(), before)

    def test_mixed_input_forms_merge_in_order(self):
        text = json.dumps(self.shard_b(), sort_keys=True, separators=(",", ":"))
        t = self.build()
        self.assertIsNone(
            t.merge_snapshots([self.shard_a(), text, text.encode("utf-8")])
        )
        counters = {
            (c["service"], c["name"], tuple(c["labels"])): c["value"]
            for c in t.snapshot()["counters"]
        }
        self.assertEqual(counters[("", "hits", (("a", 1),))], 2 + 3 + 4 + 4)
        self.assertEqual(counters[("api", "only", ())], 5)
        samples = {(s["service"], s["name"]): s for s in t.snapshot()["samples"]}
        self.assertEqual(
            samples[("api", "lat")]["values"], [1, 2.5, "3.5", "3.5"]
        )
        self.assertEqual(samples[("api", "lat")]["count"], 4)
        self.assertEqual(samples[("web", "lat")]["values"], [4, 4])
        self.assertEqual(
            {(e["service"], e["span"]) for e in t.query("open")},
            {("api", "open1")},
        )
        self.assertEqual(
            {(e["service"], e["span"]) for e in t.query("closed")},
            {("", "r1"), ("", "r2")},
        )

    def test_matches_sequential_merge_snapshot(self):
        shards = [self.shard_a(), self.shard_b(), self.shard_a()]
        combined = self.build()
        combined.merge_snapshots(shards)
        sequential = self.build()
        for shard in shards:
            sequential.merge_snapshot(shard)
        self.assertEqual(combined.snapshot(), sequential.snapshot())
        self.assertEqual(combined.json(), sequential.json())

    def test_tuple_of_payloads_accepted(self):
        t = self.build()
        self.assertIsNone(t.merge_snapshots((self.shard_a(), self.shard_b())))
        counters = {
            (c["service"], c["name"]): c["value"]
            for c in t.snapshot()["counters"]
        }
        self.assertEqual(counters[("", "hits")], 9)

    def test_identical_spans_across_shards_are_idempotent(self):
        t = self.build()
        own = {"counters": [], "samples": [], "spans": t.snapshot()["spans"]}
        before = t.snapshot()
        t.merge_snapshots([own, own, self.shard_a()])
        self.assertEqual(t.snapshot()["spans"], before["spans"] + self.shard_a()["spans"])

    def test_invalid_member_rolls_back_everything(self):
        t = self.build()
        before = t.snapshot()
        bad_cases = [
            b"\xff not utf-8",
            "{not json",
            {"counters": [], "samples": []},
            {"counters": [], "samples": [], "spans": [], "extra": 1},
        ]
        for index, bad in enumerate(bad_cases):
            with self.assertRaises(SnapshotFormatError, msg="case %d" % index):
                t.merge_snapshots([self.shard_a(), bad])
            self.assertEqual(t.snapshot(), before, msg="case %d" % index)

    def test_conflict_in_later_shard_rolls_back_everything(self):
        t = self.build()
        before = t.snapshot()
        conflict = self.shard_a()
        for record in conflict["spans"]:
            if record["span"] == "open1":
                record["parent"] = "different"
        with self.assertRaises(ValueError):
            t.merge_snapshots([self.shard_b(), self.shard_a(), conflict])
        self.assertEqual(t.snapshot(), before)

    def test_counter_add_failure_rolls_back_everything(self):
        t = Telemetry.from_snapshot({
            "counters": [
                {"service": "", "name": "c", "labels": [], "value": "ab"},
            ],
            "samples": [],
            "spans": [],
        })
        before = t.snapshot()
        good = {"counters": [
            {"service": "", "name": "d", "labels": [], "value": 1},
        ], "samples": [], "spans": []}
        bad = {"counters": [
            {"service": "", "name": "c", "labels": [], "value": 1},
        ], "samples": [], "spans": []}
        with self.assertRaises(ValueError):
            t.merge_snapshots([good, bad])
        self.assertEqual(t.snapshot(), before)

    def test_format_error_anywhere_beats_conflict(self):
        # 全部输入先完成解析与格式检查，之后才做冲突检查：即使较早的分片
        # 与当前状态冲突，较晚分片的格式问题仍按 SnapshotFormatError 抛出。
        t = self.build()
        conflict = self.shard_a()
        for record in conflict["spans"]:
            if record["span"] == "open1":
                record["parent"] = "different"
        with self.assertRaises(SnapshotFormatError):
            t.merge_snapshots([conflict, "{not json"])

    def test_clock_not_read_and_inputs_not_shared(self):
        t = self.build()
        t.clock = lambda: (_ for _ in ()).throw(AssertionError("clock read"))
        shard_a, shard_b = self.shard_a(), self.shard_b()
        copies = copy.deepcopy([shard_a, shard_b])
        t.merge_snapshots([shard_a, shard_b])
        self.assertEqual([shard_a, shard_b], copies)  # 输入未被改写
        # 改动输入内部对象不影响已合并的实例
        shard_a["counters"][0]["value"] = 999
        shard_b["samples"][0]["values"].append(999)
        again = self.build()
        again.clock = t.clock
        again.merge_snapshots(copies)
        self.assertEqual(t.snapshot(), again.snapshot())


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


class TelemetryCriticalPathTest(unittest.TestCase):
    def _tree(self):
        # root 0..30；a 1..11（自身 10）；b 2..20，b1 6..16（b 链 18+10=28）
        clock = iter([0, 1, 11, 2, 6, 16, 20, 30]).__next__
        t = Telemetry(clock)
        t.start("root")
        t.start("a", parent="root")
        t.finish("a")
        t.start("b", parent="root")
        t.start("b1", parent="b")
        t.finish("b1")
        t.finish("b")
        t.finish("root")
        return t

    def test_picks_longest_descendant_chain(self):
        t = self._tree()
        result = t.trace_critical_path("root")
        self.assertEqual(set(result), {"span", "service", "duration", "spans"})
        self.assertEqual(result["span"], "root")
        self.assertEqual(result["service"], "")
        self.assertEqual([s["span"] for s in result["spans"]],
                         ["root", "b", "b1"])
        self.assertEqual([s["duration"] for s in result["spans"]],
                         [30.0, 18.0, 10.0])
        # 整条链 duration 自 0.0 起按数组顺序累加。
        self.assertEqual(result["duration"], 58.0)
        self.assertIsInstance(result["duration"], float)
        for item in result["spans"]:
            self.assertIsInstance(item["duration"], float)

    def test_records_match_query_fields(self):
        t = self._tree()
        result = t.trace_critical_path("root")
        closed = {(e["service"], e["span"]): e for e in t.query("closed")}
        for item in result["spans"]:
            entry = closed[(item["service"], item["span"])]
            for field in ("span", "service", "parent", "start", "end",
                          "error"):
                self.assertEqual(item[field], entry[field])
            self.assertNotIn("labels", item)  # 无标签记录不附加 labels
            self.assertEqual(item["duration"],
                             float(entry["end"]) - float(entry["start"]))

    def test_empty_chain_contains_only_self(self):
        t = Telemetry(iter([0, 5]).__next__)
        t.start("root")
        t.finish("root")
        result = t.trace_critical_path("root")
        self.assertEqual([s["span"] for s in result["spans"]], ["root"])
        self.assertEqual(result["duration"], 5.0)

    def test_tie_breaks_by_start_then_span(self):
        # 两条子链耗时总和相同：开始时间更早者胜。
        t = Telemetry(iter([0, 1, 5, 6, 10, 20]).__next__)
        t.start("root")
        t.start("a", parent="root")
        t.finish("a")  # dur 4
        t.start("b", parent="root")
        t.finish("b")  # dur 4
        t.finish("root")
        self.assertEqual(
            [s["span"] for s in t.trace_critical_path("root")["spans"]],
            ["root", "a"],
        )
        # 开始时间也相同时按跨度标识稳定排序。
        t2 = Telemetry(iter([0, 1, 5, 1, 5, 20]).__next__)
        t2.start("root")
        t2.start("b", parent="root")
        t2.finish("b")
        t2.start("a", parent="root")
        t2.finish("a")
        t2.finish("root")
        self.assertEqual(
            [s["span"] for s in t2.trace_critical_path("root")["spans"]],
            ["root", "a"],
        )

    def test_missing_root_returns_none(self):
        t = self._tree()
        self.assertIsNone(t.trace_critical_path("nope"))
        self.assertIsNone(t.trace_critical_path("root", service="web"))

    def test_invalid_arguments(self):
        t = self._tree()
        for bad in ("", 1, b"x"):
            with self.assertRaises(ValueError):
                t.trace_critical_path("root", service=bad)
        for bad in (["root"], {"s": 1}):
            with self.assertRaises(ValueError):
                t.trace_critical_path(bad)

    def test_open_root_returns_none(self):
        t = Telemetry(iter([0, 1]).__next__)
        t.start("root")
        t.start("open", parent="root")
        self.assertIsNone(t.trace_critical_path("root"))

    def test_open_node_and_descendants_excluded(self):
        # 根已结束；c 开放（其已结束子节点 d 不进入链路），走另一子链 a。
        t = Telemetry(iter([0, 1, 2, 3, 4, 9, 20]).__next__)
        t.start("root")
        t.start("c", parent="root")
        t.start("d", parent="c")
        t.finish("d")  # c 仍开放：c/d 整条不进入链路
        t.start("a", parent="root")
        t.finish("a")
        t.finish("root")
        result = t.trace_critical_path("root")
        self.assertEqual([s["span"] for s in result["spans"]], ["root", "a"])

    def test_closed_descendant_under_open_node_still_validated(self):
        # d 在开放节点 c 之下、不进入链路，但其 end 不可转换仍须 ValueError。
        class BadFloat:
            def __float__(self):
                raise RuntimeError("boom")

        t = Telemetry(iter([0, 1, 2]).__next__)
        t.start("root")
        t.start("c", parent="root")
        t.start("d", parent="c")
        t.spans[("", "d")]["end"] = BadFloat()
        with self.assertRaises(ValueError):
            t.trace_critical_path("root")
        # 开放节点自身的时间戳不被转换：给它一个不可转换 start 也不报错。
        t.spans[("", "c")]["start"] = BadFloat()
        with self.assertRaises(ValueError):
            t.trace_critical_path("root")
        # d 修复后开放 c 的坏 start 不再被读取，查询返回 None（根开放）。
        t.spans[("", "d")]["end"] = 3
        self.assertIsNone(t.trace_critical_path("root"))

    def test_non_finite_timestamps_rejected(self):
        t = Telemetry(iter([0, float("inf")]).__next__)
        t.start("root")
        t.finish("root")
        with self.assertRaises(ValueError):
            t.trace_critical_path("root")
        t2 = Telemetry(iter([0, 10]).__next__)
        t2.start("root")
        t2.finish("root")
        t2.spans[("", "root")]["start"] = float("nan")
        with self.assertRaises(ValueError):
            t2.trace_critical_path("root")

    def test_cycle_raises_without_partial_result(self):
        t = Telemetry(iter(range(10)).__next__)
        t.start("a", parent="b")
        t.start("b", parent="a")
        with self.assertRaises(ValueError):
            t.trace_critical_path("a")
        t2 = Telemetry(iter(range(10)).__next__)
        t2.start("self", parent="self")
        with self.assertRaises(ValueError):
            t2.trace_critical_path("self")

    def test_service_isolation_and_dangling_parent(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("root")
        t.finish("root")
        t.start("root", service="api")
        t.start("x", parent="root", service="api")
        t.finish("x", service="api")
        t.finish("root", service="api")
        t.start("orphan", parent="ghost")  # 父标识不存在，不可达
        # 默认服务树不含 api 子节点。
        self.assertEqual(
            [s["span"] for s in t.trace_critical_path("root")["spans"]],
            ["root"],
        )
        result = t.trace_critical_path("root", service="api")
        self.assertEqual(result["service"], "api")
        self.assertEqual([s["span"] for s in result["spans"]], ["root", "x"])

    def test_labels_and_error_preserved(self):
        err = RuntimeError("boom")
        t = Telemetry(iter([0, 1, 10, 20]).__next__)
        t.start("root", labels=(("k", [1, 2]),))
        t.start("a", parent="root")
        t.finish("a", error=err)
        t.finish("root")
        result = t.trace_critical_path("root")
        self.assertIs(result["spans"][1]["error"], err)
        self.assertEqual(result["spans"][0]["labels"], [("k", [1, 2])])

    def test_result_is_independent_and_read_only(self):
        t = self._tree()
        before = t.snapshot()
        clock = t.clock
        result = t.trace_critical_path("root")
        result["spans"][0]["span"] = "mutated"
        result["spans"][0]["start"] = 999
        result["spans"].clear()
        result["duration"] = -1
        self.assertEqual(t.snapshot(), before)  # 聚合器未被回写
        self.assertIs(t.clock, clock)
        again = t.trace_critical_path("root")
        self.assertEqual([s["span"] for s in again["spans"]],
                         ["root", "b", "b1"])
        self.assertEqual(again["duration"], 58.0)
        # labels 跨次调用互不共享。
        t2 = Telemetry(iter([0, 5]).__next__)
        t2.start("root", labels=(("k", "v"),))
        t2.finish("root")
        first = t2.trace_critical_path("root")
        first["spans"][0]["labels"].append(("z", 1))
        second = t2.trace_critical_path("root")
        self.assertEqual(second["spans"][0]["labels"], [("k", "v")])

    def test_tuple_span_ids(self):
        t = Telemetry(iter([0, 1, 5, 6, 10, 20]).__next__)
        t.start(("r", 0))
        t.start(("a", 1), parent=("r", 0))
        t.finish(("a", 1))
        t.start(("b", 2), parent=("r", 0))
        t.finish(("b", 2))
        t.finish(("r", 0))
        result = t.trace_critical_path(("r", 0))
        self.assertEqual([s["span"] for s in result["spans"]],
                         [("r", 0), ("a", 1)])


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
        # 标签筛选同样作用于跨度：build 中的跨度全部无标签，
        # 非空标签筛选不命中任何跨度
        self.assertEqual(
            t.snapshot(labels=(("a", 1), ("b", 2)))["spans"],
            [],
        )
        # 显式空标签命中全部无标签跨度
        self.assertEqual(
            t.snapshot(labels=())["spans"],
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


class TelemetryDiffTest(unittest.TestCase):
    def build_before(self):
        t = Telemetry(iter(range(100)).__next__)
        t.inc("hits", 2)
        t.inc("gone", 9, service="api")
        t.observe("lat", 1, service="api")
        t.observe("lat", 2.5, service="api")
        t.start("r1")
        t.finish("r1", error="boom")
        t.start("open1", service="api")
        return t

    def build_after(self):
        t = Telemetry(iter(range(200)).__next__)
        t.inc("hits", 5)                              # counter changed
        t.inc("new", 7)                               # counter added
        t.observe("lat", 1, service="api")
        t.observe("lat", 2.5, service="api")
        t.observe("lat", "3.5", service="api")        # sample changed
        t.start("r1")
        t.finish("r1", error="boom")                  # identical span
        t.start("open1", service="api")
        t.finish("open1", service="api")              # open -> closed
        t.start("added-span", service="web")
        return t

    def test_top_level_shape_and_empty_diff(self):
        t = self.build_before()
        d = Telemetry.diff_snapshots(t.snapshot(), t.snapshot())
        self.assertEqual(set(d), {"counters", "samples", "spans"})
        for section in d.values():
            self.assertEqual(set(section), {"added", "removed", "changed"})
            self.assertEqual(section, {"added": [], "removed": [], "changed": []})
        # 自己与自己（JSON 文本形式）也无差异
        self.assertEqual(
            Telemetry.diff_snapshots(t.json(), t.json()),
            {"counters": {"added": [], "removed": [], "changed": []},
             "samples": {"added": [], "removed": [], "changed": []},
             "spans": {"added": [], "removed": [], "changed": []}},
        )

    def test_added_removed_changed_classification(self):
        before = self.build_before().snapshot()
        after = self.build_after().snapshot()
        d = Telemetry.diff_snapshots(before, after)

        self.assertEqual(
            [(c["service"], c["name"]) for c in d["counters"]["added"]],
            [("", "new")],
        )
        self.assertEqual(
            [(c["service"], c["name"]) for c in d["counters"]["removed"]],
            [("api", "gone")],
        )
        changed = d["counters"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["before"]["value"], 2)
        self.assertEqual(changed[0]["after"]["value"], 5)
        self.assertEqual(
            {c["name"] for c in changed[0].values()}, {"hits"}
        )
        self.assertEqual(set(changed[0]), {"before", "after"})

        self.assertEqual(d["samples"]["added"], [])
        self.assertEqual(d["samples"]["removed"], [])
        sample_changed = d["samples"]["changed"]
        self.assertEqual(len(sample_changed), 1)
        self.assertEqual(sample_changed[0]["before"]["values"], [1, 2.5])
        self.assertEqual(sample_changed[0]["after"]["values"], [1, 2.5, "3.5"])

        self.assertEqual(
            [(x["service"], x["span"]) for x in d["spans"]["added"]],
            [("web", "added-span")],
        )
        self.assertEqual(d["spans"]["removed"], [])
        span_changed = d["spans"]["changed"]
        self.assertEqual(len(span_changed), 1)
        entry = span_changed[0]
        self.assertEqual(entry["before"]["span"], "open1")
        self.assertIsNone(entry["before"]["end"])
        self.assertIsNotNone(entry["after"]["end"])
        # 完全一致的跨度 r1 不出现在任何数组
        for bucket in ("added", "removed", "changed"):
            self.assertNotIn(
                "r1",
                [x.get("span", x.get("after", {}).get("span"))
                 for x in d["spans"][bucket]],
            )

    def test_accepts_dict_text_and_bytes(self):
        before = self.build_before()
        after = self.build_after()
        expected = Telemetry.diff_snapshots(before.snapshot(), after.snapshot())
        variants = (
            (before.json(), after.json()),
            (before.json().encode("utf-8"), after.json().encode("utf-8")),
            (before.snapshot(), after.json()),
            (before.json().encode("utf-8"), after.snapshot()),
        )
        for raw_before, raw_after in variants:
            self.assertEqual(
                Telemetry.diff_snapshots(raw_before, raw_after), expected
            )

    def test_sample_stats_renormalized_before_comparison(self):
        base = {"counters": [], "spans": []}
        no_stats = dict(base, samples=[
            {"service": "", "name": "m", "labels": [], "values": [1, 2.5]},
        ])
        with_stats = dict(base, samples=[{
            "service": "", "name": "m", "labels": [], "values": [1, 2.5],
            "count": 2, "sum": 3.5, "minimum": 1.0,
            "maximum": 2.5, "mean": 1.75,
        }])
        # 携带与省略等价统计、键序不同都不应报变化
        self.assertEqual(
            Telemetry.diff_snapshots(no_stats, with_stats)["samples"],
            {"added": [], "removed": [], "changed": []},
        )
        self.assertEqual(
            Telemetry.diff_snapshots(with_stats, no_stats)["samples"],
            {"added": [], "removed": [], "changed": []},
        )
        # changed 记录始终带按公开规则重算的完整统计（before 省略也补齐）
        grown = dict(base, samples=[
            {"service": "", "name": "m", "labels": [], "values": [1, 2.5, 4]},
        ])
        changed = Telemetry.diff_snapshots(no_stats, grown)["samples"]["changed"]
        self.assertEqual(len(changed), 1)
        for side in ("before", "after"):
            self.assertEqual(
                set(changed[0][side]),
                {"service", "name", "labels", "values",
                 "count", "sum", "minimum", "maximum", "mean"},
            )
        self.assertEqual(changed[0]["before"]["mean"], 1.75)
        # 标签键序不同但归一化后相同，不算不同记录
        reordered = dict(base, samples=[{
            "service": "", "name": "m",
            "labels": [["b", 2], ["a", 1]],
            "values": [1, 2.5],
        }])
        canonical = dict(base, samples=[{
            "service": "", "name": "m",
            "labels": [["a", 1], ["b", 2]],
            "values": [1, 2.5],
        }])
        self.assertEqual(
            Telemetry.diff_snapshots(reordered, canonical)["samples"]["changed"],
            [],
        )

    def test_span_field_changes_count_as_changed(self):
        head = {"counters": [], "samples": []}
        open_span = {"span": "s", "service": "", "parent": None,
                     "start": 0, "end": None, "error": None}
        cases = [
            dict(open_span, end=1),                    # open -> closed
            dict(open_span, parent="p"),               # parent 变化
            dict(open_span, error="e"),                # error 变化（仍 open）
            dict(open_span, start=5),                  # start 变化
        ]
        for changed_record in cases:
            d = Telemetry.diff_snapshots(
                dict(head, spans=[open_span]),
                dict(head, spans=[changed_record]),
            )
            self.assertEqual(len(d["spans"]["changed"]), 1, msg=changed_record)
            self.assertEqual(d["spans"]["added"], [])
            self.assertEqual(d["spans"]["removed"], [])
        # error=0（假值非 None）与 None 也算变化
        d = Telemetry.diff_snapshots(
            dict(head, spans=[dict(open_span, end=1, error=None)]),
            dict(head, spans=[dict(open_span, end=1, error=0)]),
        )
        self.assertEqual(len(d["spans"]["changed"]), 1)

    def test_ordering_added_removed_follow_snapshots_changed_follows_after(self):
        b = Telemetry(iter(range(100)).__next__)
        b.inc("n", service="s2")
        b.inc("n", service="s1")
        b.start("x", service="s2")
        b.start("x", service="s1")
        a_empty = Telemetry().snapshot()
        d = Telemetry.diff_snapshots(a_empty, b.snapshot())
        # added 沿用 after 快照排序：计数器与跨度都以 service 为首键
        self.assertEqual(
            [c["service"] for c in d["counters"]["added"]], ["s1", "s2"]
        )
        self.assertEqual(
            [x["service"] for x in d["spans"]["added"]], ["s1", "s2"]
        )
        # 同服务内跨度再按开始时间排序
        b2 = Telemetry(iter(range(100)).__next__)
        b2.start("late", service="s")   # start 0
        b2.start("early", service="s")  # start 1
        d_time = Telemetry.diff_snapshots(
            Telemetry().snapshot(), b2.snapshot()
        )
        self.assertEqual(
            [x["span"] for x in d_time["spans"]["added"]], ["late", "early"]
        )
        # removed 沿用 before 快照排序
        d2 = Telemetry.diff_snapshots(b.snapshot(), a_empty)
        self.assertEqual(
            [c["service"] for c in d2["counters"]["removed"]], ["s1", "s2"]
        )
        # changed 按 after 的快照排序
        before = {"counters": [
            {"service": "s2", "name": "n", "labels": [], "value": 1},
            {"service": "s1", "name": "n", "labels": [], "value": 1},
        ], "samples": [], "spans": []}
        after = {"counters": [
            {"service": "s2", "name": "n", "labels": [], "value": 2},
            {"service": "s1", "name": "n", "labels": [], "value": 2},
        ], "samples": [], "spans": []}
        d3 = Telemetry.diff_snapshots(before, after)
        self.assertEqual(
            [c["after"]["service"] for c in d3["counters"]["changed"]],
            ["s1", "s2"],
        )

    def test_invalid_inputs_raise_valueerror_without_partial_results(self):
        good = self.build_before().snapshot()
        bad_payloads = [
            "{not json}",
            b"\xff not utf-8",
            42,
            None,
            [1, 2],
            {"counters": [], "samples": []},                    # 缺 spans
            dict(good, extra=[]),                               # 多余顶层键
            '{"counters":[],"counters":[],"samples":[],"spans":[]}',
            {"counters": [{"service": "", "name": "c",
                           "labels": [["k", float("nan")]], "value": 1}],
             "samples": [], "spans": []},
            {"counters": [], "samples": [], "spans": [
                {"span": ["unhashable"], "service": "", "parent": None,
                 "start": 0, "end": None, "error": None}]},
            {"counters": [
                {"service": "", "name": "c", "labels": [], "value": 1},
                {"service": "", "name": "c", "labels": [], "value": 2},
            ], "samples": [], "spans": []},
        ]
        for bad in bad_payloads:
            with self.assertRaises(ValueError, msg=repr(bad)):
                Telemetry.diff_snapshots(bad, good)
            with self.assertRaises(ValueError, msg=repr(bad)):
                Telemetry.diff_snapshots(good, bad)

    def test_readonly_does_not_touch_inputs_clock_or_network(self):
        before = self.build_before()
        after = self.build_after()
        before_snap = copy.deepcopy(before.snapshot())
        after_snap = copy.deepcopy(after.snapshot())

        class AssertingClock:
            reads = 0

            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read by diff")

        before.clock = AssertingClock()
        after.clock = AssertingClock()
        d1 = Telemetry.diff_snapshots(before_snap, after_snap)
        d2 = Telemetry.diff_snapshots(before.json(), after.json())
        self.assertEqual(AssertingClock.reads, 0)
        # 输入快照与实例状态均不变
        self.assertEqual(before_snap, before.snapshot())
        self.assertEqual(after_snap, after.snapshot())
        self.assertEqual(before.snapshot(), self.build_before().snapshot())
        json.dumps(d1, allow_nan=False)  # 结果严格可 JSON 序列化
        json.dumps(d2, allow_nan=False)

    def test_result_is_independent_from_inputs(self):
        before = self.build_before().snapshot()
        after = self.build_after().snapshot()
        before_copy = copy.deepcopy(before)
        after_copy = copy.deepcopy(after)
        d = Telemetry.diff_snapshots(before, after)
        # 改写结果中的记录与嵌套标签不影响输入，也不影响再次 diff
        d["counters"]["changed"][0]["before"]["value"] = 999
        d["counters"]["added"][0]["labels"].append(("zzz", 0))
        d["samples"]["changed"][0]["after"]["values"].append(999)
        d["spans"]["changed"][0]["after"]["parent"] = "mutated"
        d["spans"]["added"] = []
        self.assertEqual(before, before_copy)
        self.assertEqual(after, after_copy)
        again = Telemetry.diff_snapshots(before_copy, after_copy)
        self.assertEqual(len(again["spans"]["added"]), 1)
        self.assertEqual(
            again["counters"]["changed"][0]["before"]["value"], 2
        )
        # before/after 两份记录彼此独立
        pair = again["counters"]["changed"][0]
        self.assertIsNot(pair["before"], pair["after"])
        pair["before"]["name"] = "x"
        self.assertNotEqual(pair["after"]["name"], "x")


class TelemetryRestoreResumeTest(unittest.TestCase):
    EMPTY = {"counters": [], "samples": [], "spans": []}

    def events(self):
        return [
            {"op": "inc", "name": "requests", "labels": (("b", 2), ("a", 1))},
            {"op": "inc", "name": "requests", "value": -3, "service": "api"},
            {"op": "observe", "name": "lat", "value": 1, "service": "api"},
            {"op": "observe", "name": "lat", "value": "2.5", "service": "api"},
            {"op": "start", "span": "root"},
            {"op": "start", "span": "child", "parent": "root"},
            {"op": "finish", "span": "child", "error": "boom"},
            {"op": "observe", "name": "lat", "value": 4, "service": "api"},
            {"op": "inc", "name": "requests"},
            {"op": "finish", "span": "root"},
            {"op": "start", "span": "late", "parent": "root"},
        ]

    def apply(self, t, events):
        for event in events:
            kwargs = {k: v for k, v in event.items() if k != "op"}
            getattr(t, event["op"])(**kwargs)

    def test_error_type_is_public_valueerror_subclass(self):
        self.assertTrue(issubclass(SnapshotFormatError, ValueError))
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore("{not json}")
        # 既有按 ValueError 捕获的用法继续有效
        with self.assertRaises(ValueError):
            Telemetry.restore("{not json}")

    def test_resume_equals_full_recording(self):
        events = self.events()
        split = 6
        full_values = iter(range(1000))
        t_full = Telemetry(lambda: next(full_values))
        self.apply(t_full, events)

        part_values = iter(range(1000))
        t_part = Telemetry(lambda: next(part_values))
        self.apply(t_part, events[:split])
        snap_text = t_part.json()
        # 恢复本身不读取 clock：续采实例从同一序列继续取时间戳
        t_resumed = Telemetry.restore(snap_text, clock=lambda: next(part_values))
        self.apply(t_resumed, events[split:])

        self.assertEqual(t_resumed.snapshot(), t_full.snapshot())
        self.assertEqual(t_resumed.json(), t_full.json())
        for status in ("open", "closed", "error"):
            self.assertEqual(t_resumed.query(status), t_full.query(status))
        self.assertEqual(t_resumed.trace("root"), t_full.trace("root"))
        # 未结束跨度不因恢复丢失
        self.assertEqual([e["span"] for e in t_resumed.query("open")], ["late"])

    def test_restore_accepts_dict_text_and_bytes(self):
        t = Telemetry(iter(range(100)).__next__)
        self.apply(t, self.events())
        expected = t.json()
        for raw in (t.snapshot(), t.json(), t.json().encode("utf-8")):
            restored = Telemetry.restore(raw)
            self.assertEqual(restored.json(), expected)
            self.assertEqual(restored.snapshot(), t.snapshot())

    def test_restore_snapshot_instance_method_is_atomic(self):
        t = Telemetry(iter(range(100)).__next__)
        t.inc("keep", 5)
        t.start("open1")
        before = t.snapshot()

        # 失败恢复：原聚合器状态完全不变
        bad_cases = [
            "{not json}",
            "[1, 2]",
            42,
            dict(self.EMPTY, extra=1),
            dict(self.EMPTY, version=2),
            dict(self.EMPTY, counters={}),
            dict(self.EMPTY, spans=[{"span": "s", "service": "", "parent": "ghost",
                                     "start": 0, "end": None, "error": None}]),
        ]
        for bad in bad_cases:
            with self.assertRaises(SnapshotFormatError, msg=repr(bad)):
                t.restore_snapshot(bad)
            self.assertEqual(t.snapshot(), before, msg=repr(bad))

        # 成功恢复：整体替换为快照状态，返回 None
        source = Telemetry(iter(range(1000)).__next__)
        source.inc("other", 2, service="api")
        source.start("s", service="api")
        self.assertIsNone(t.restore_snapshot(source.json()))
        self.assertEqual(t.snapshot(), source.snapshot())
        self.assertEqual(t.json(), source.json())
        # 替换后可继续记录
        t.finish("s", service="api")
        self.assertEqual(t.query("open"), [])
        self.assertEqual(len(t.query("closed")), 1)

    def test_version_validation(self):
        # 缺省（既有格式）与显式版本 1 都可读取
        for payload in (
            self.EMPTY,
            dict(self.EMPTY, version=1),
            json.dumps(dict(self.EMPTY, version=1)),
        ):
            t = Telemetry.restore(payload)
            self.assertEqual(t.snapshot(), self.EMPTY)
        for bad_version in (0, 2, -1, 99, "1", 1.0, True, None, [1]):
            payload = dict(self.EMPTY, version=bad_version)
            with self.assertRaises(SnapshotFormatError, msg=repr(bad_version)):
                Telemetry.restore(payload)
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore('{"version":2,"counters":[],"samples":[],"spans":[]}')
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore('{"version":1.0,"counters":[],"samples":[],"spans":[]}')
        # 既有入口保持既有行为：version 对 from_snapshot 仍是多余字段
        with self.assertRaises(ValueError):
            Telemetry.from_snapshot(dict(self.EMPTY, version=1))

    def test_empty_snapshot_gives_usable_empty_aggregator(self):
        for payload in (self.EMPTY, dict(self.EMPTY, version=1), json.dumps(self.EMPTY)):
            t = Telemetry.restore(payload)
            self.assertEqual(t.snapshot(), self.EMPTY)
            self.assertEqual(t.query("open"), [])
            t.inc("x")
            t.observe("m", 1.5)
            t.start("s")
            t.finish("s")
            self.assertEqual(t.snapshot()["counters"][0]["value"], 1)
            self.assertEqual(t.query("closed")[0]["span"], "s")

    def test_parent_reference_validation(self):
        def snap_with(spans):
            return dict(self.EMPTY, spans=spans)

        base = {"span": "a", "service": "", "parent": None,
                "start": 0, "end": None, "error": None}
        # 悬空引用：父标识不存在
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore(snap_with([dict(base, parent="ghost")]))
        # 父标识只存在于其他服务：按 trace 的既定语义同样不是有效父子引用
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore(snap_with([
                dict(base),
                dict(base, span="b", service="api", parent="a"),
            ]))
        # 不可哈希的父标识无法引用任何跨度
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore(snap_with([dict(base, parent=["x"])]))
        # 自环与互环
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore(snap_with([dict(base, parent="a")]))
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore(snap_with([
                dict(base, parent="b"),
                dict(base, span="b", parent="a"),
            ]))
        # 同服务内合法的父子链可以恢复，父子关系保留
        t = Telemetry.restore(snap_with([
            dict(base),
            dict(base, span="b", parent="a", start=1),
            dict(base, span="c", parent="b", start=2),
        ]))
        tree = t.trace("a")
        self.assertEqual([c["span"] for c in tree["children"]], ["b"])
        self.assertEqual(
            [c["span"] for c in tree["children"][0]["children"]], ["c"]
        )
        # from_snapshot 保持既有宽松行为：不检查父子引用
        legacy = Telemetry.from_snapshot(snap_with([dict(base, parent="ghost")]))
        self.assertEqual(legacy.query("open")[0]["parent"], "ghost")

    def test_duplicate_span_ids_rejected(self):
        span = {"span": "s", "service": "", "parent": None,
                "start": 0, "end": None, "error": None}
        with self.assertRaises(SnapshotFormatError):
            Telemetry.restore(dict(self.EMPTY, spans=[span, dict(span)]))
        # 同标识不同服务是不同跨度，合法
        t = Telemetry.restore(dict(self.EMPTY, spans=[
            span, dict(span, service="api"),
        ]))
        self.assertEqual(len(t.query("open")), 2)

    def test_format_errors_all_raise_snapshot_format_error(self):
        counter = {"service": "", "name": "c", "labels": [], "value": 1}
        sample = {"service": "", "name": "m", "labels": [], "values": [1.0]}
        span = {"span": "s", "service": "", "parent": None,
                "start": 0, "end": None, "error": None}
        cases = [
            "{not json",
            b"\xff not utf-8",
            "[1, 2]",
            42,
            None,
            {"counters": [], "samples": []},                 # 缺 spans
            dict(self.EMPTY, counters={}),                   # 类型错误
            dict(self.EMPTY, counters=[dict(counter, value=float("nan"))]),
            dict(self.EMPTY, counters=[dict(counter, value=float("inf"))]),
            dict(self.EMPTY, samples=[dict(sample, values=[float("-inf")])]),
            dict(self.EMPTY, samples=[dict(sample, values=[1.0], count=2)]),
            dict(self.EMPTY, spans=[dict(span, start=float("nan"))]),
            dict(self.EMPTY, spans=[dict(span, span=["unhashable"])]),
            dict(self.EMPTY, counters=[dict(counter, labels=[["k", 1], ["k", 2]])]),
            dict(self.EMPTY, counters=[dict(counter, service=1)]),
            dict(self.EMPTY, counters=[dict(counter)] * 2),  # 重复记录
            '{"counters":[],"counters":[],"samples":[],"spans":[]}',
            '{"counters":[],"samples":[],"spans":[],"x":NaN}',
        ]
        for payload in cases:
            with self.assertRaises(SnapshotFormatError, msg=repr(payload)):
                Telemetry.restore(payload)
            t = Telemetry()
            with self.assertRaises(SnapshotFormatError, msg=repr(payload)):
                t.restore_snapshot(payload)
            self.assertEqual(t.snapshot(), self.EMPTY)

    def test_other_restore_entries_raise_public_error_too(self):
        with self.assertRaises(SnapshotFormatError):
            Telemetry.from_snapshot("{bad")
        with self.assertRaises(SnapshotFormatError):
            Telemetry().merge_snapshot("{bad")
        with self.assertRaises(SnapshotFormatError):
            Telemetry.diff_snapshots("{bad", self.EMPTY)
        with self.assertRaises(SnapshotFormatError):
            Telemetry.diff_snapshots(self.EMPTY, "{bad")

    def test_two_instances_same_events_byte_identical(self):
        t = Telemetry(iter(range(100)).__next__)
        self.apply(t, self.events())
        snap_text = t.json()

        clock_a = iter(range(100, 200)).__next__
        a = Telemetry.restore(snap_text, clock=clock_a)
        b = Telemetry(iter(range(100, 200)).__next__)
        self.assertIsNone(b.restore_snapshot(snap_text))
        followup = [
            {"op": "inc", "name": "requests", "value": 2},
            {"op": "observe", "name": "lat", "value": 9, "service": "api"},
            {"op": "finish", "span": "late", "error": "late-error"},
            {"op": "start", "span": "new"},
        ]
        self.apply(a, followup)
        self.apply(b, followup)
        for status in ("open", "closed", "error"):
            self.assertEqual(a.query(status), b.query(status))
        self.assertEqual(a.json(), b.json())
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_finish_after_restore_only_ends_that_span(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("a")
        t.start("b")
        t.start("c", service="api")
        restored = Telemetry.restore(t.json(), clock=lambda: 42.0)
        self.assertEqual(
            {(e["service"], e["span"]) for e in restored.query("open")},
            {("", "a"), ("", "b"), ("api", "c")},
        )
        restored.finish("a", error="done")
        # 只结束 a：b 与 c 仍未结束，也不会被隐式结束
        self.assertEqual(
            {(e["service"], e["span"]) for e in restored.query("open")},
            {("", "b"), ("api", "c")},
        )
        errored = restored.query("error")
        self.assertEqual([(e["service"], e["span"]) for e in errored], [("", "a")])
        self.assertEqual(errored[0]["end"], 42.0)

    def test_unicode_empty_and_extreme_numbers_roundtrip(self):
        t = Telemetry()
        t.inc("n", -3, labels=(("", "空值"), ("键", "🚀")), service="服务")
        t.inc("n", 0)  # 缺省即默认服务（空服务名）
        t.observe("m", 5e-324, service="服务")
        t.observe("m", -0.0, service="服务")
        t.observe("m", -7.25, service="服务")
        text = t.json()
        first = Telemetry.restore(text)
        second = Telemetry.restore(text)
        self.assertEqual(first.json(), text)
        self.assertEqual(second.json(), text)
        self.assertEqual(first.json(), second.json())
        sample = first.snapshot()["samples"][0]
        self.assertEqual(sample["values"], [5e-324, -0.0, -7.25])
        self.assertEqual(sample["minimum"], -7.25)
        counters = {
            (c["service"], c["value"]) for c in first.snapshot()["counters"]
        }
        self.assertEqual(counters, {("服务", -3), ("", 0)})

    def test_restore_does_not_mutate_input(self):
        t = Telemetry(iter(range(100)).__next__)
        self.apply(t, self.events())
        payload = t.snapshot()
        saved = copy.deepcopy(payload)
        Telemetry.restore(payload)
        self.assertEqual(payload, saved)
        # 恢复后的实例与输入不共享可变对象
        payload["counters"][0]["value"] = 999
        payload["samples"][0]["values"].append(999)
        restored = Telemetry.restore(saved)
        self.assertNotEqual(
            restored.snapshot()["counters"][0]["value"], 999
        )
        self.assertEqual(
            len(restored.snapshot()["samples"][0]["values"]), 3
        )


class TelemetrySpanResultIsolationTest(unittest.TestCase):
    """只读跨度结果的可变值别名隔离：query/snapshot/trace 返回的记录、
    parent 与 error 中的可变容器必须与内部状态及同次结果的其他记录
    互不共享；异常与自定义对象保持入口的原值语义。"""

    def build(self):
        clock = iter(range(100)).__next__
        t = Telemetry(clock)
        t.start("a", parent=["root", {"tag": [1, 2]}])
        t.start("b", parent=["root", {"tag": [1, 2]}])
        t.finish("a", error={"code": [1, 2]})
        t.start("s1", service="api", parent={"p": ["q"]})
        t.finish("s1", service="api", error=["boom", {"k": "v"}])
        t.start("open1", parent=["o"])
        return t

    def test_query_records_do_not_alias_internal_state(self):
        t = self.build()
        before = t.json()
        for rec in t.query("closed"):
            if isinstance(rec["error"], dict):
                rec["error"]["code"].append(999)
            if isinstance(rec["error"], list):
                rec["error"].append("x")
            if isinstance(rec["parent"], dict):
                rec["parent"]["p"].append("x")
        for rec in t.query("open"):
            rec["parent"].append("x")
        self.assertEqual(t.query("open")[0]["parent"], ["root", {"tag": [1, 2]}])
        self.assertEqual(t.query("error")[0]["error"], {"code": [1, 2]})
        self.assertEqual(t.query("error")[1]["error"], ["boom", {"k": "v"}])
        self.assertEqual(t.json(), before)  # JSON 输出不受影响

    def test_snapshot_records_do_not_alias_internal_state(self):
        t = self.build()
        before = t.json()
        snap = t.snapshot()
        for rec in snap["spans"]:
            if isinstance(rec["parent"], list):
                rec["parent"].append("S")
            if isinstance(rec["error"], dict):
                rec["error"]["code"] = []
        self.assertEqual(t.snapshot(), self.build().snapshot())
        self.assertEqual(t.json(), before)

    def test_same_result_records_do_not_share_containers(self):
        t = Telemetry(iter(range(100)).__next__)
        shared = ["shared"]
        t.start("x", parent=shared)
        t.start("y", parent=shared)
        result = t.query("open")
        result[0]["parent"].append("ONE")
        self.assertEqual(result[1]["parent"], ["shared"])
        self.assertEqual(t.query("open")[0]["parent"], ["shared"])

    def test_trace_tree_does_not_alias_internal_state(self):
        t = self.build()
        before = t.json()
        node = t.trace("b")
        node["parent"].append("T")
        node["children"].append("junk")
        api = t.trace("s1", service="api")
        api["error"].append("T")
        api["parent"]["p"].append("T")
        self.assertEqual(t.trace("b")["parent"], ["root", {"tag": [1, 2]}])
        self.assertEqual(
            t.trace("s1", service="api")["error"], ["boom", {"k": "v"}]
        )
        self.assertEqual(t.json(), before)

    def test_uncopyable_objects_keep_original_value_semantics(self):
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise RuntimeError("nope")

            def __copy__(self):
                raise RuntimeError("nope")

        t = Telemetry(iter(range(100)).__next__)
        marker = Uncopyable()
        t.start("u", parent=[marker, {"a": 1}])
        t.finish("u", error=marker)
        rec = t.query("closed")[0]
        self.assertIs(rec["error"], marker)  # 不可复制对象原值返回
        self.assertIs(rec["parent"][0], marker)
        rec["parent"].append("Z")  # 容器本身仍与内部状态隔离
        rec["parent"][1]["a"] = 9
        self.assertEqual(t.query("closed")[0]["parent"], [marker, {"a": 1}])
        node = t.trace("u")  # trace 不因不可复制对象新增拒绝
        self.assertIs(node["error"], marker)

    def test_restored_and_merged_spans_are_isolated(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("root")
        t.start("child", parent="root")
        t.finish("child", error={"code": [1]})
        restored = Telemetry.restore(t.json(), clock=iter(range(100)).__next__)
        rec = restored.query("closed")[0]
        rec["error"]["code"].append(9)
        self.assertEqual(restored.query("closed")[0]["error"], {"code": [1]})
        merged = Telemetry(iter(range(100)).__next__)
        merged.merge_snapshot(t.snapshot())
        rec = merged.query("closed")[0]
        rec["error"]["code"].append(9)
        self.assertEqual(merged.query("closed")[0]["error"], {"code": [1]})


class TelemetryHistogramTest(unittest.TestCase):
    def test_bucketing_left_open_right_closed(self):
        t = Telemetry(clock=lambda: 0.0)
        for v in (0, 1, 2, 2, 5, 9, 10, 10.5, 11):
            t.observe("x", v)
        # <=1 : 0,1 ; (1,5] : 2,2,5 ; (5,10] : 9,10 ; >10 : 10.5,11
        result = t.histogram("x", [1, 5, 10])
        self.assertEqual(
            result,
            {"boundaries": [1, 5, 10], "counts": [2, 3, 2, 2], "count": 9},
        )
        self.assertIsInstance(result["boundaries"], list)
        self.assertIsInstance(result["counts"], list)
        self.assertTrue(all(type(c) is int for c in result["counts"]))
        self.assertEqual(result["count"], sum(result["counts"]))
        self.assertEqual(len(result["counts"]), len(result["boundaries"]) + 1)

    def test_values_on_boundaries_go_to_closed_side(self):
        t = Telemetry()
        for v in (1, 5, 10):
            t.observe("y", v)
        self.assertEqual(t.histogram("y", [1, 5, 10])["counts"], [1, 1, 1, 0])

    def test_single_boundary_and_negative_values(self):
        t = Telemetry()
        t.observe("z", -3)
        t.observe("z", 3)
        self.assertEqual(
            t.histogram("z", [0]),
            {"boundaries": [0], "counts": [1, 1], "count": 2},
        )

    def test_tuple_boundaries_and_int_float_mix(self):
        t = Telemetry()
        for v in (0, 1, 2, 2, 5, 9, 10, 10.5, 11):
            t.observe("x", v)
        result = t.histogram("x", (1, 5.0, 10))
        self.assertEqual(result["boundaries"], [1, 5.0, 10])
        self.assertEqual(result["counts"], [2, 3, 2, 2])

    def test_missing_series_wrong_service_or_labels_returns_none(self):
        t = Telemetry()
        t.observe("x", 1)
        self.assertIsNone(t.histogram("missing", [0]))
        self.assertIsNone(t.histogram("x", [0], service="api"))
        self.assertIsNone(t.histogram("x", [0], labels=(("a", 1),)))

    def test_default_service_and_label_normalization(self):
        t = Telemetry()
        t.observe("d", 5)  # 默认服务
        self.assertEqual(t.histogram("d", [4, 6])["counts"], [0, 1, 0])
        t.observe("l", 1, labels=(("b", 2), ("a", 1)))
        result = t.histogram("l", [0, 2], labels=(("a", 1), ("b", 2)))
        self.assertEqual(result["counts"], [0, 1, 0])

    def test_invalid_boundaries_raise_valueerror(self):
        t = Telemetry()
        t.observe("x", 1)
        bad = [
            None, [], (), "1,2", {1: 2}, 5, [1, True], [True, 2],
            [1, "2"], [1, 0.0], [1, float("nan")], [float("inf")],
            [1, 1], [2, 1], [1, 2, 2], [1, 2.0, 2],
        ]
        for boundaries in bad:
            with self.assertRaises(ValueError):
                t.histogram("x", boundaries)

    def test_invalid_boundaries_raise_even_for_missing_series(self):
        t = Telemetry()
        for boundaries in ([], [2, 1], [1, 1], [1, float("nan")]):
            with self.assertRaises(ValueError):
                t.histogram("missing", boundaries)

    def test_invalid_name_service_labels_raise_valueerror(self):
        t = Telemetry()
        t.observe("x", 1)
        with self.assertRaises(ValueError):
            t.histogram(["x"], [0])  # 不可哈希 name
        for service in ("", 1, b"x"):
            with self.assertRaises(ValueError):
                t.histogram("x", [0], service=service)
        with self.assertRaises(ValueError):
            t.histogram("x", [0], labels=(("a", 1), ("a", 2)))  # 重复键
        with self.assertRaises(ValueError):
            t.histogram("x", [0], labels=(("a", object()),))  # 不可序列化

    def test_boundary_validation_before_reading_samples_and_without_clock(self):
        calls = []
        t = Telemetry(clock=lambda: (calls.append(1) or 0.0))
        t.observe("x", 1)
        for boundaries in ([1, 1], [], [float("nan")]):
            with self.assertRaises(ValueError):
                t.histogram("x", boundaries)
        self.assertEqual(calls, [])  # 任何拒绝都不读 clock

    def test_non_finite_existing_data_raises_without_partial_result(self):
        t = Telemetry()
        t.observe("g", 1)
        t.samples[("", "g", ())].append(float("nan"))
        with self.assertRaises(ValueError):
            t.histogram("g", [0])
        # 内部状态不变，可继续对正常序列查询
        t.observe("ok", 1)
        self.assertEqual(t.histogram("ok", [0])["counts"], [0, 1])

    def test_string_numeric_values_converted_like_observe(self):
        t = Telemetry()
        t.observe("s", "2.5")
        self.assertEqual(t.histogram("s", [1, 2, 3])["counts"], [0, 0, 1, 0])

    def test_result_is_independent_fresh_each_call(self):
        t = Telemetry()
        for v in (0, 1, 2, 2, 5, 9, 10, 10.5, 11):
            t.observe("x", v)
        boundaries = [1, 5, 10]
        first = t.histogram("x", boundaries)
        first["boundaries"].append(999)
        first["counts"][0] = 999
        first["count"] = -1
        boundaries.append(100)  # 调用方容器之后被改也不影响再次查询
        second = t.histogram("x", [1, 5, 10])
        self.assertEqual(
            second,
            {"boundaries": [1, 5, 10], "counts": [2, 3, 2, 2], "count": 9},
        )
        self.assertIsNot(second["counts"], first["counts"])
        self.assertIsNot(second["boundaries"], first["boundaries"])

    def test_readonly_leaves_aggregation_snapshot_json_untouched(self):
        t = Telemetry()
        for v in (0, 1, 2, 2, 5, 9, 10, 10.5, 11):
            t.observe("x", v)
        snapshot_before = t.snapshot()
        json_before = t.json()
        values_before = list(t.samples[("", "x", ())])
        t.histogram("x", [1, 5, 10])
        t.histogram("x", [0, 3, 7, 11])
        self.assertEqual(t.snapshot(), snapshot_before)
        self.assertEqual(t.json(), json_before)
        self.assertEqual(list(t.samples[("", "x", ())]), values_before)
        # 不新增快照字段
        self.assertEqual(set(snapshot_before), {"counters", "samples", "spans"})

    def test_histogram_after_restore_merge(self):
        t = Telemetry()
        for v in (0, 1, 2, 2, 5, 9, 10, 10.5, 11):
            t.observe("x", v)
        restored = Telemetry.restore(t.snapshot())
        self.assertEqual(
            restored.histogram("x", [1, 5, 10]),
            {"boundaries": [1, 5, 10], "counts": [2, 3, 2, 2], "count": 9},
        )
        merged = Telemetry()
        merged.merge_snapshot(t.json())
        self.assertEqual(
            merged.histogram("x", (1, 5, 10))["counts"], [2, 3, 2, 2]
        )


class TelemetrySpanLabelsTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("plain")                                  # 默认服务，无标签
        t.start("tagged", labels=(("k", "v"),))           # 默认服务，有标签
        t.start("other", labels=(("k", "w"),), service="api")
        return t

    def test_start_labels_normalized_and_caller_untouched(self):
        t = Telemetry(iter(range(1000)).__next__)
        lab = [["b", [2, 1]], ("a", {"x": 1})]
        t.start("s", labels=lab, service="api")
        # 调用方的可变对象不被修改
        self.assertEqual(lab, [["b", [2, 1]], ("a", {"x": 1})])
        entry = t.query("open")[0]
        # 键顺序归一；数组/对象值按原结构带回
        self.assertEqual(entry["labels"], [("a", {"x": 1}), ("b", [2, 1])])
        # 返回的 labels 可安全修改，不回流到聚合器
        entry["labels"][0][1]["x"] = 999
        self.assertEqual(
            t.query("open")[0]["labels"], [("a", {"x": 1}), ("b", [2, 1])]
        )

    def test_unlabeled_span_keeps_existing_shape(self):
        t = self.build()
        for entry in t.query("open", labels=()):
            self.assertEqual(
                set(entry),
                {"span", "service", "parent", "start", "end", "error"},
            )

    def test_finish_does_not_rewrite_labels(self):
        t = self.build()
        t.finish("tagged")
        closed = t.query("closed", labels=(("k", "v"),))
        self.assertEqual([e["span"] for e in closed], ["tagged"])
        self.assertEqual(closed[0]["labels"], [("k", "v")])

    def test_start_labels_validation(self):
        t = Telemetry(iter(range(1000)).__next__)
        for bad in (
            [("a", 1), ("a", 2)],          # 重复键
            [("a", float("nan"))],         # 非严格 JSON
            "not-pairs",                   # 非成对输入
            [("a", object())],             # 不可序列化值
        ):
            with self.assertRaises(ValueError):
                t.start("s", labels=bad)
        # 非法标签不推进 clock、不留半条记录
        self.assertEqual(t.query("open"), [])
        # 重复开始（含带标签）仍拒绝，原标签不被覆盖
        t.start("s", labels=(("k", "v"),))
        with self.assertRaises(ValueError):
            t.start("s", labels=(("k", "other"),))
        self.assertEqual(t.query("open")[0]["labels"], [("k", "v")])

    def test_query_labels_filter(self):
        t = self.build()
        # 省略筛选匹配全部
        self.assertEqual(
            [e["span"] for e in t.query("open")],
            ["plain", "tagged", "other"],
        )
        self.assertEqual(t.query("open"), t.query("open", labels=None))
        # 显式空标签只命中无标签记录
        self.assertEqual([e["span"] for e in t.query("open", labels=())], ["plain"])
        # 规范化键值精确匹配（输入顺序无关）
        self.assertEqual(
            [e["span"] for e in t.query("open", labels=(("k", "v"),))],
            ["tagged"],
        )
        # 与服务筛选组合
        self.assertEqual(
            [e["span"] for e in t.query("open", service="api", labels=(("k", "w"),))],
            ["other"],
        )
        # 无匹配返回空列表
        self.assertEqual(t.query("open", labels=(("k", "missing"),)), [])
        # 非法筛选抛 ValueError，状态不变
        with self.assertRaises(ValueError):
            t.query("open", labels=[("a", 1), ("a", 1)])
        self.assertEqual(len(t.query("open")), 3)

    def test_snapshot_and_json_labels_filter_spans(self):
        t = self.build()
        snap = t.snapshot(labels=(("k", "v"),))
        self.assertEqual([s["span"] for s in snap["spans"]], ["tagged"])
        self.assertEqual(snap["spans"][0]["labels"], [("k", "v")])
        # 显式空标签命中无标签跨度，无标签条目保持既有字段形状
        empty = t.snapshot(labels=())
        self.assertEqual([s["span"] for s in empty["spans"]], ["plain"])
        self.assertNotIn("labels", empty["spans"][0])
        # json 与 snapshot 同口径
        parsed = json.loads(t.json(labels=(("k", "w"),)))
        self.assertEqual([s["span"] for s in parsed["spans"]], ["other"])
        self.assertEqual(parsed["spans"][0]["labels"], [["k", "w"]])

    def test_trace_nodes_carry_labels(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("root", labels=(("role", "root"),))
        t.start("child", parent="root", labels=(("role", "leaf"),))
        t.start("plain-child", parent="root")
        tree = t.trace("root")
        self.assertEqual(tree["labels"], [("role", "root")])
        children = {c["span"]: c for c in tree["children"]}
        self.assertEqual(children["child"]["labels"], [("role", "leaf")])
        self.assertNotIn("labels", children["plain-child"])
        # 返回的 labels 可安全修改
        tree["children"][0]["labels"].append(("hack", 1))
        self.assertEqual(
            t.trace("root")["children"][0]["labels"], [("role", "leaf")]
        )

    def test_snapshot_roundtrip_with_labels(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("a", labels=(("x", 1),))
        t.finish("a")
        t.start("b")
        snap = t.snapshot()
        spans = {s["span"]: s for s in snap["spans"]}
        self.assertEqual(spans["a"]["labels"], [("x", 1)])
        self.assertNotIn("labels", spans["b"])
        for payload in (snap, t.json()):
            restored = Telemetry.restore(payload)
            self.assertEqual(restored.snapshot(), snap)
            resumed = Telemetry(iter(range(1000, 2000)).__next__)
            resumed.restore_snapshot(payload)
            self.assertEqual(resumed.snapshot(), snap)

    def test_restore_missing_and_invalid_labels(self):
        base = {"span": "s", "service": "", "parent": None,
                "start": 1, "end": None, "error": None}
        empty = {"counters": [], "samples": [], "spans": [base]}
        # 缺少 labels 的旧快照按空标签恢复
        restored = Telemetry.from_snapshot(empty)
        self.assertNotIn("labels", restored.snapshot()["spans"][0])
        # 显式空 labels 等价于无标签
        explicit = {"counters": [], "samples": [],
                    "spans": [dict(base, labels=[])]}
        self.assertNotIn(
            "labels", Telemetry.from_snapshot(explicit).snapshot()["spans"][0]
        )
        # 非法 labels 一律 SnapshotFormatError
        for bad in (
            dict(base, labels=[("a", 1), ("a", 2)]),
            dict(base, labels="x"),
            dict(base, labels=[("a", float("nan"))]),
            dict(base, labels=[("a", 1)], extra=1),
        ):
            with self.assertRaises(SnapshotFormatError):
                Telemetry.from_snapshot(
                    {"counters": [], "samples": [], "spans": [bad]}
                )

    def test_merge_label_conflict_atomic(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("m", labels=(("k", "v"),))
        snap = t.snapshot()
        other = Telemetry(iter(range(1000)).__next__)
        other.merge_snapshot(snap)
        self.assertEqual(other.snapshot(), snap)  # 一致时幂等
        conflict = copy.deepcopy(snap)
        conflict["spans"][0]["labels"] = [["k", "other"]]
        before = other.snapshot()
        with self.assertRaises(ValueError):
            other.merge_snapshot(conflict)
        self.assertEqual(other.snapshot(), before)  # 原子失败，无部分状态

    def test_diff_label_change_is_changed(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("m", labels=(("k", "v"),))
        before = t.snapshot()
        after = copy.deepcopy(before)
        after["spans"][0]["labels"] = [["k", "other"]]
        diff = Telemetry.diff_snapshots(before, after)
        self.assertEqual(diff["spans"]["added"], [])
        self.assertEqual(diff["spans"]["removed"], [])
        self.assertEqual(len(diff["spans"]["changed"]), 1)
        changed = diff["spans"]["changed"][0]
        self.assertEqual(changed["before"]["labels"], [("k", "v")])
        self.assertEqual(changed["after"]["labels"], [("k", "other")])
        # labels 筛选作用于 spans：after 侧标签不匹配时按 removed 判定
        filtered = Telemetry.diff_snapshots(before, after, labels=(("k", "v"),))
        self.assertEqual(len(filtered["spans"]["removed"]), 1)
        self.assertEqual(filtered["spans"]["changed"], [])

    def test_batch_start_labels(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.batch([
            {"op": "start", "span": "b1", "labels": (("z", 1),)},
            {"op": "finish", "span": "b1"},
        ])
        self.assertEqual(t.query("closed")[0]["labels"], [("z", 1)])
        # 非法 labels 整批拒绝，不留部分状态
        with self.assertRaises(ValueError):
            t.batch([
                {"op": "start", "span": "b2", "labels": [("a", 1), ("a", 2)]},
            ])
        self.assertEqual(t.query("open"), [])
        # 未知字段仍拒绝
        with self.assertRaises(ValueError):
            t.batch([{"op": "finish", "span": "b1", "labels": ()}])

    def test_invalid_filter_does_not_read_clock(self):
        calls = []
        t = Telemetry(lambda: calls.append(1) or 0.0)
        with self.assertRaises(ValueError):
            t.snapshot(labels=[("a", 1), ("a", 1)])
        with self.assertRaises(ValueError):
            t.query("open", labels="bad")
        with self.assertRaises(ValueError):
            t.start("s", labels=[("a", 1), ("a", 1)])
        self.assertEqual(calls, [])


class TelemetrySpanDurationStatsTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("a")                          # start 0
        t.finish("a", error="boom")          # end 1   dur 1.0, error
        t.start("b", service="api")          # start 2
        t.finish("b", service="api")         # end 3   dur 1.0, closed 无异常
        t.start("c", service="api")          # start 4, 仍 open
        t.start("d", service="api", labels=(("k", "v"),))  # start 5
        t.finish("d", service="api", error=0)             # end 6 dur 1.0, error
        return t

    def test_closed_stats_shape_and_values(self):
        t = self.build()
        stats = t.span_duration_stats()
        self.assertEqual(
            set(stats),
            {"values", "count", "sum", "minimum", "maximum", "mean"},
        )
        # 顺序沿用 query/snapshot 的服务、开始时间、标识顺序。
        self.assertEqual(
            [e["span"] for e in t.query("closed")],
            ["a", "b", "d"],
        )
        self.assertEqual(stats["values"], [1.0, 1.0, 1.0])
        self.assertTrue(all(type(v) is float for v in stats["values"]))
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["sum"], 3.0)
        self.assertEqual(stats["minimum"], 1.0)
        self.assertEqual(stats["maximum"], 1.0)
        self.assertEqual(stats["mean"], 1.0)

    def test_values_follow_snapshot_order_with_varied_durations(self):
        clock = iter([0, 10,    # 服务 b 的 z：dur 10
                      2, 5,     # api a：dur 3
                      4, 7]).__next__  # api m：dur 3
        t = Telemetry(clock)
        t.start("z", service="b")
        t.finish("z", service="b")
        t.start("a", service="api")
        t.finish("a", service="api")
        t.start("m", service="api")
        t.finish("m", service="api")
        # 服务序 api < b：api 的两个跨度按 start 排在 b 的 z 之前。
        self.assertEqual(
            [e["span"] for e in t.snapshot(status="closed")["spans"]],
            ["a", "m", "z"],
        )
        stats = t.span_duration_stats()
        self.assertEqual(stats["values"], [3.0, 3.0, 10.0])
        self.assertEqual(stats["sum"], 16.0)
        self.assertEqual(stats["minimum"], 3.0)
        self.assertEqual(stats["maximum"], 10.0)
        self.assertEqual(stats["mean"], 16.0 / 3)

    def test_sum_accumulates_in_value_order(self):
        # values 顺序（非数值大小顺序）决定累加顺序。
        clock = iter([10, 12,    # api q：dur 2
                      0, 1]).__next__  # 默认服务 p：dur 1
        t = Telemetry(clock)
        t.start("q", service="api")
        t.finish("q", service="api")
        t.start("p")
        t.finish("p")
        # 默认服务排在 api 前：values 为 [1.0, 2.0]。
        stats = t.span_duration_stats()
        self.assertEqual(stats["values"], [1.0, 2.0])
        self.assertEqual(stats["sum"], 3.0)
        self.assertEqual(stats["mean"], 1.5)

    def test_error_status_only_includes_non_none_error(self):
        t = self.build()
        stats = t.span_duration_stats(status="error")
        # a（error="boom"）与 d（error=0，假值非 None）；b 正常结束不计。
        self.assertEqual([e["span"] for e in t.query("error")], ["a", "d"])
        self.assertEqual(stats["values"], [1.0, 1.0])
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["sum"], 2.0)

    def test_invalid_status_raises_valueerror(self):
        t = self.build()
        for bad in ("open", "", "CLOSED", "done", None, 0, b"closed", ["closed"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.span_duration_stats(status=bad)

    def test_service_and_labels_filters_match_query_semantics(self):
        t = self.build()
        self.assertEqual(t.span_duration_stats(service="api")["count"], 2)
        self.assertEqual(
            t.span_duration_stats(service="api")["values"],
            [e["end"] - e["start"] for e in t.query("closed", service="api")],
        )
        self.assertIsNone(t.span_duration_stats(service="missing"))
        # 空字符串表示默认服务。
        self.assertEqual(t.span_duration_stats(service="")["values"], [1.0])
        # 显式空标签只命中无标签已结束跨度（a、b），跨所有服务。
        self.assertEqual(t.span_duration_stats(labels=())["count"], 2)
        self.assertEqual(
            t.span_duration_stats(service="api", labels=())["values"], [1.0]
        )
        self.assertEqual(
            t.span_duration_stats(labels=(("k", "v"),))["values"], [1.0]
        )
        self.assertIsNone(t.span_duration_stats(labels=(("k", "missing"),)))
        self.assertEqual(
            t.span_duration_stats(service="api", status="error")["count"], 1
        )

    def test_no_ended_spans_returns_none(self):
        # 完全没有跨度
        self.assertIsNone(Telemetry().span_duration_stats())
        # 只有未结束跨度：closed/error 都返回 None
        t = Telemetry()
        t.start("o")
        self.assertIsNone(t.span_duration_stats())
        self.assertIsNone(t.span_duration_stats(status="error"))
        # 未结束跨度仍可通过原入口查询
        self.assertEqual([e["span"] for e in t.query("open")], ["o"])

    def test_invalid_service_and_labels_raise_valueerror(self):
        t = self.build()
        for bad in (1, b"api", ["api"], True, object()):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.span_duration_stats(service=bad)
        with self.assertRaises(ValueError):
            t.span_duration_stats(labels=(("k", 1), ("k", 2)))  # 重复键
        with self.assertRaises(ValueError):
            t.span_duration_stats(labels=(("v", object()),))   # 不可序列化
        with self.assertRaises(ValueError):
            t.span_duration_stats(labels=(("v", float("nan")),))
        with self.assertRaises(ValueError):
            t.span_duration_stats(labels="bad")
        # 即使服务筛选本身无匹配，非法标签仍在读取数据前拒绝
        with self.assertRaises(ValueError):
            t.span_duration_stats(service="nope", labels="bad")

    def test_validation_and_conversion_do_not_read_clock_or_mutate(self):
        class AssertingClock:
            reads = 0

            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read")

        t = self.build()
        before = t.snapshot()
        t.clock = AssertingClock()
        t.span_duration_stats()
        t.span_duration_stats(status="error")
        t.span_duration_stats(service="api", labels=())
        for bad in ("open", "done", None, 0):
            with self.assertRaises(ValueError):
                t.span_duration_stats(status=bad)
        with self.assertRaises(ValueError):
            t.span_duration_stats(service=1)
        self.assertEqual(AssertingClock.reads, 0)
        t.clock = iter(range(100)).__next__
        self.assertEqual(t.snapshot(), before)

    def test_non_finite_timestamps_raise_without_partial_result(self):
        def state_with(start, end, error=None):
            t = Telemetry()
            t.spans[("", "s")] = {
                "parent": None, "start": start, "end": end,
                "error": error, "labels": (),
            }
            return t

        for start, end in (
            (0, "not-a-time"),
            ("nope", 1),
            (0, float("nan")),
            (float("inf"), 1),
            (object(), 1),
            (0, 10 ** 400),
        ):
            t = state_with(start, end)
            before = t.snapshot()
            with self.assertRaises(ValueError, msg=(start, end)):
                t.span_duration_stats()
            self.assertEqual(t.snapshot(), before)

        # 一条正常 + 一条异常：整体 ValueError，不返回部分结果
        t = Telemetry(iter(range(100)).__next__)
        t.start("good")
        t.finish("good")
        t.spans[("", "bad")] = {
            "parent": None, "start": 0, "end": "nope",
            "error": None, "labels": (),
        }
        with self.assertRaises(ValueError):
            t.span_duration_stats()

        # 未结束跨度的非法时间戳不参与转换，不触发错误
        t = state_with("nope", None)
        self.assertIsNone(t.span_duration_stats())

        # 数字字符串沿用样本浮点规则正常转换
        t = state_with("1", "4.5")
        self.assertEqual(t.span_duration_stats()["values"], [3.5])

    def test_result_is_fresh_independent_and_repeatable(self):
        t = self.build()
        first = t.span_duration_stats()
        second = t.span_duration_stats()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["values"], second["values"])
        first["values"].append(999.0)
        first["count"] = 0
        first["sum"] = -1.0
        again = t.span_duration_stats()
        self.assertEqual(again["count"], 3)
        self.assertEqual(again["sum"], 3.0)
        self.assertEqual(again["values"], [1.0, 1.0, 1.0])

    def test_does_not_change_snapshot_json_or_error_rules(self):
        t = self.build()
        before = t.snapshot()
        t.span_duration_stats()
        t.span_duration_stats(status="error")
        # 不新增任何快照字段
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(set(t.snapshot()), {"counters", "samples", "spans"})
        for record in t.snapshot()["spans"]:
            self.assertIn(
                set(record),
                (
                    {"span", "service", "parent", "start", "end", "error"},
                    {"span", "service", "parent", "start", "end", "error",
                     "labels"},
                ),
            )
        # 异常对象仍按原入口原值保留
        errored = t.query("error")
        self.assertEqual(errored[0]["error"], "boom")
        self.assertEqual(errored[1]["error"], 0)

    def test_after_restore_merge_and_from_snapshot(self):
        base = self.build()
        snap = base.snapshot()
        expected = base.span_duration_stats()
        self.assertEqual(Telemetry.restore(base.json()).span_duration_stats(), expected)
        self.assertEqual(
            Telemetry.from_snapshot(snap).span_duration_stats(), expected
        )
        merged = Telemetry()
        self.assertIsNone(merged.merge_snapshot(base.snapshot()))
        self.assertEqual(merged.span_duration_stats(), expected)

    def test_exception_instance_error_preserved(self):
        t = Telemetry(iter(range(10)).__next__)
        t.start("e")
        t.finish("e", error=ValueError("x"))
        stats = t.span_duration_stats(status="error")
        self.assertEqual(stats["values"], [1.0])
        self.assertIsInstance(t.query("error")[0]["error"], ValueError)
        # JSON 占位规则不受影响
        self.assertEqual(
            json.loads(t.json(status="error"))["spans"][0]["error"],
            {"type": "ValueError", "message": "x"},
        )


class TelemetrySpansByDurationTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("a")                          # start 0
        t.finish("a", error="boom")          # end 1   dur 1.0, error
        t.start("b", service="api")          # start 2
        t.finish("b", service="api")         # end 3   dur 1.0, closed 无异常
        t.start("c", service="api")          # start 4, 仍 open
        t.start("d", service="api", labels=(("k", "v"),))  # start 5
        t.finish("d", service="api", error=0)             # end 6 dur 1.0, error
        return t

    def test_result_shape_fields_and_duration_type(self):
        t = self.build()
        rows = t.spans_by_duration()
        # 未结束跨度 c 不返回；排序沿用 snapshot 的服务、start、span。
        self.assertEqual([e["span"] for e in rows], ["a", "b", "d"])
        for row in rows:
            self.assertIn(
                set(row),
                (
                    {"span", "service", "parent", "start", "end", "error",
                     "duration"},
                    {"span", "service", "parent", "start", "end", "error",
                     "labels", "duration"},
                ),
            )
            self.assertIs(type(row["duration"]), float)
            self.assertEqual(row["duration"], 1.0)
        # 有标签跨度保留 labels，无标签跨度不带该字段。
        self.assertEqual(rows[0].get("labels"), None)
        self.assertEqual(rows[2]["labels"], [("k", "v")])
        # 每项与 query 同形（仅多 duration）。
        for row, entry in zip(rows, t.query("closed")):
            for field in ("span", "service", "parent", "start", "end", "error"):
                self.assertEqual(row[field], entry[field])

    def test_closed_interval_boundaries_inclusive(self):
        t = Telemetry(iter([0, 1,    # p1：dur 1
                            2, 5,    # p3：dur 3
                            4, 14]).__next__)  # p10：dur 10
        t.start("p1"); t.finish("p1")
        t.start("p3"); t.finish("p3")
        t.start("p10"); t.finish("p10")
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(minimum=3, maximum=10)],
            ["p3", "p10"],
        )
        # 边界闭区间：恰好等于边界的耗时被包含。
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(minimum=3, maximum=3)],
            ["p3"],
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(maximum=3)], ["p1", "p3"]
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(minimum=3)], ["p3", "p10"]
        )
        # 省略两侧即无界。
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration()], ["p1", "p3", "p10"]
        )
        self.assertEqual([e["duration"] for e in t.spans_by_duration()],
                         [1.0, 3.0, 10.0])
        # 区间内无匹配返回空列表。
        self.assertEqual(t.spans_by_duration(minimum=4, maximum=9), [])

    def test_status_error_only_non_none_error(self):
        t = self.build()
        rows = t.spans_by_duration(status="error")
        self.assertEqual([e["span"] for e in rows], ["a", "d"])
        self.assertTrue(all(e["error"] is not None for e in rows))

    def test_invalid_status_raises_valueerror(self):
        t = self.build()
        for bad in ("open", "", "CLOSED", "done", None, 0, b"closed", ["x"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.spans_by_duration(status=bad)

    def test_bounds_must_be_non_bool_finite_int_or_float(self):
        t = self.build()
        for bad in (
            True, False, "1", b"1", 1j, object(),
            float("nan"), float("inf"), float("-inf"),
        ):
            with self.assertRaises(ValueError, msg=("minimum", repr(bad))):
                t.spans_by_duration(minimum=bad)
            with self.assertRaises(ValueError, msg=("maximum", repr(bad))):
                t.spans_by_duration(maximum=bad)
        # int（含 0、负数、超大整数）与有限 float 合法。
        t.spans_by_duration(minimum=0, maximum=10 ** 100)
        t.spans_by_duration(minimum=-5.0, maximum=0.0)
        t.spans_by_duration(minimum=1, maximum=1)

    def test_minimum_greater_than_maximum_raises(self):
        t = self.build()
        for lo, hi in ((2, 1), (1.1, 1.0), (10 ** 100, 1)):
            with self.assertRaises(ValueError, msg=(lo, hi)):
                t.spans_by_duration(minimum=lo, maximum=hi)

    def test_service_and_labels_filters_match_query_semantics(self):
        t = self.build()
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(service="api")], ["b", "d"]
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(service="")], ["a"]
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(labels=())], ["a", "b"]
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(labels=(("k", "v"),))],
            ["d"],
        )
        self.assertEqual(t.spans_by_duration(labels=(("k", "missing"),)), [])
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(
                service="api", status="error")],
            ["d"],
        )
        # 区间与筛选叠加。
        self.assertEqual(
            t.spans_by_duration(service="api", minimum=2.0), []
        )

    def test_invalid_service_and_labels_raise_valueerror(self):
        t = self.build()
        for bad in (1, b"api", ["api"], True, object()):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.spans_by_duration(service=bad)
        with self.assertRaises(ValueError):
            t.spans_by_duration(labels=(("k", 1), ("k", 2)))  # 重复键
        with self.assertRaises(ValueError):
            t.spans_by_duration(labels=(("v", object()),))    # 不可序列化
        with self.assertRaises(ValueError):
            t.spans_by_duration(labels=(("v", float("nan")),))
        with self.assertRaises(ValueError):
            t.spans_by_duration(labels="bad")
        # 全部条件先校验：区间矛盾 + 非法标签同时存在仍是 ValueError，
        # 且即使服务本身无匹配，非法标签也在读取数据前拒绝。
        with self.assertRaises(ValueError):
            t.spans_by_duration(minimum=5, maximum=1, labels="bad")
        with self.assertRaises(ValueError):
            t.spans_by_duration(service="nope", labels="bad")

    def test_validation_and_conversion_do_not_read_clock_or_mutate(self):
        class AssertingClock:
            reads = 0

            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read")

        t = self.build()
        before = t.snapshot()
        t.clock = AssertingClock()
        t.spans_by_duration()
        t.spans_by_duration(status="error")
        t.spans_by_duration(minimum=1.0, maximum=1.0, service="api",
                            labels=())
        for bad in ("open", "done", None, 0):
            with self.assertRaises(ValueError):
                t.spans_by_duration(status=bad)
        with self.assertRaises(ValueError):
            t.spans_by_duration(service=1)
        with self.assertRaises(ValueError):
            t.spans_by_duration(minimum=float("inf"))
        with self.assertRaises(ValueError):
            t.spans_by_duration(minimum=9, maximum=1)
        self.assertEqual(AssertingClock.reads, 0)
        t.clock = iter(range(100)).__next__
        self.assertEqual(t.snapshot(), before)

    def test_non_finite_timestamps_raise_without_partial_result(self):
        def state_with(start, end, error=None):
            x = Telemetry()
            x.spans[("", "s")] = {
                "parent": None, "start": start, "end": end,
                "error": error, "labels": (),
            }
            return x

        for start, end in (
            (0, "not-a-time"),
            ("nope", 1),
            (0, float("nan")),
            (float("inf"), 1),
            (object(), 1),
            (0, 10 ** 400),
        ):
            x = state_with(start, end)
            before = x.snapshot()
            with self.assertRaises(ValueError, msg=(start, end)):
                x.spans_by_duration()
            self.assertEqual(x.snapshot(), before)

        # 一条正常 + 一条异常：全部候选先转换，整体 ValueError 无部分结果；
        # 即使异常跨度落在请求区间之外也一样先转换。
        x = Telemetry()
        x.spans[("", "bad")] = {
            "parent": None, "start": 0, "end": "nope",
            "error": None, "labels": (),
        }
        x.spans[("", "good")] = {
            "parent": None, "start": 0, "end": 100.0,
            "error": None, "labels": (),
        }
        with self.assertRaises(ValueError):
            x.spans_by_duration(minimum=50.0, maximum=60.0)

        # 未结束跨度不转换也不返回：其非法时间戳不触发错误。
        x = state_with("nope", None)
        self.assertEqual(x.spans_by_duration(), [])
        self.assertEqual(x.spans_by_duration(status="error"), [])

        # 数字字符串沿用样本浮点规则正常转换。
        x = state_with("1", "4.5")
        self.assertEqual(x.spans_by_duration()[0]["duration"], 3.5)

    def test_result_is_fresh_independent_and_repeatable(self):
        t = self.build()
        first = t.spans_by_duration()
        second = t.spans_by_duration()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[2]["labels"], second[2]["labels"])
        first[0]["duration"] = 999.0
        first[0]["error"] = "changed"
        first[2]["labels"].append(("z", 1))
        first.append("junk")
        again = t.spans_by_duration()
        self.assertEqual(len(again), 3)
        self.assertEqual([e["duration"] for e in again], [1.0, 1.0, 1.0])
        self.assertEqual(again[0]["error"], "boom")
        self.assertEqual(again[2]["labels"], [("k", "v")])

    def test_exception_instance_error_preserved(self):
        t = Telemetry(iter(range(10)).__next__)
        t.start("e")
        t.finish("e", error=ValueError("x"))
        row = t.spans_by_duration(status="error")[0]
        self.assertEqual(row["duration"], 1.0)
        self.assertIsInstance(row["error"], ValueError)
        self.assertEqual(
            json.loads(t.json(status="error"))["spans"][0]["error"],
            {"type": "ValueError", "message": "x"},
        )

    def test_open_spans_and_empty_aggregator(self):
        self.assertEqual(Telemetry().spans_by_duration(), [])
        t = Telemetry()
        t.start("o")
        self.assertEqual(t.spans_by_duration(), [])
        self.assertEqual(t.spans_by_duration(status="error"), [])
        # 未结束跨度仍可通过原入口查询。
        self.assertEqual([e["span"] for e in t.query("open")], ["o"])

    def test_duration_not_written_to_snapshot_or_json(self):
        t = self.build()
        before = t.snapshot()
        t.spans_by_duration()
        t.spans_by_duration(status="error")
        t.spans_by_duration(minimum=0.0, maximum=100.0, service="api",
                            labels=(("k", "v"),))
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(set(t.snapshot()), {"counters", "samples", "spans"})
        for record in t.snapshot()["spans"]:
            self.assertNotIn("duration", record)
        payload = json.loads(t.json())
        self.assertTrue(all("duration" not in r for r in payload["spans"]))
        # 既有 span_duration_stats 结果不受影响。
        self.assertEqual(t.span_duration_stats()["values"], [1.0, 1.0, 1.0])

    def test_after_restore_and_merge(self):
        base = self.build()
        expected = [
            (e["span"], e["duration"]) for e in base.spans_by_duration()
        ]
        restored = Telemetry.restore(base.json()).spans_by_duration()
        self.assertEqual(
            [(e["span"], e["duration"]) for e in restored], expected
        )
        from_snap = Telemetry.from_snapshot(base.snapshot()).spans_by_duration()
        self.assertEqual(
            [(e["span"], e["duration"]) for e in from_snap], expected
        )
        merged = Telemetry()
        self.assertIsNone(merged.merge_snapshot(base.snapshot()))
        self.assertEqual(
            [(e["span"], e["duration"]) for e in merged.spans_by_duration()],
            expected,
        )

    def test_order_follows_service_start_span(self):
        t = Telemetry(iter([10, 12,    # api q：dur 2
                            0, 1,      # 默认服务 p：dur 1
                            2, 5,      # api a：dur 3
                            4, 7]).__next__)  # api m：dur 3
        t.start("q", service="api"); t.finish("q", service="api")
        t.start("p"); t.finish("p")
        t.start("a", service="api"); t.finish("a", service="api")
        t.start("m", service="api"); t.finish("m", service="api")
        self.assertEqual(
            [(e["service"], e["span"], e["duration"])
             for e in t.spans_by_duration()],
            [("", "p", 1.0), ("api", "a", 3.0),
             ("api", "m", 3.0), ("api", "q", 2.0)],
        )


class TelemetryCapacityTest(unittest.TestCase):
    # ------------------------------------------------------------------
    # max_series 参数校验
    # ------------------------------------------------------------------

    def test_invalid_max_series_raises_valueerror(self):
        # bool 是 int 的子类必须显式排除；浮点数、负数、字符串、字节、
        # 列表等统一抛普通 ValueError。
        for bad in (-1, 1.0, 0.0, True, False, "1", b"1", [1], 1 + 0j):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Telemetry(max_series=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                Telemetry.restore({"counters": [], "samples": [], "spans": []},
                                  max_series=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                Telemetry.from_snapshot(
                    {"counters": [], "samples": [], "spans": []},
                    max_series=bad,
                )

    def test_valid_limits_accepted(self):
        # 0 合法（禁止任何新序列）；None 与省略都表示不设上限。
        self.assertEqual(Telemetry(max_series=0).capacity(),
                         {"limit": 0, "used": 0, "remaining": 0})
        self.assertEqual(Telemetry(max_series=3).capacity(),
                         {"limit": 3, "used": 0, "remaining": 3})
        self.assertEqual(Telemetry().capacity(),
                         {"limit": None, "used": 0, "remaining": None})
        self.assertEqual(Telemetry(max_series=None).capacity(),
                         {"limit": None, "used": 0, "remaining": None})

    # ------------------------------------------------------------------
    # capacity() 只读视图
    # ------------------------------------------------------------------

    def test_capacity_counts_counters_and_samples_separately(self):
        t = Telemetry(max_series=10)
        t.inc("m")                      # 计数器序列
        t.observe("m", 1)               # 同名样本序列分别计数
        t.inc("m", 5)                   # 已有键，used 不变
        t.observe("m", 2)
        self.assertEqual(t.capacity(),
                         {"limit": 10, "used": 2, "remaining": 8})
        # 服务/标签任一不同都是新序列
        t.inc("m", service="api")
        t.inc("m", labels=(("a", 1),))
        t.observe("m", 1, labels=(("b", 1),))
        self.assertEqual(t.capacity()["used"], 5)
        # 标签键序归一化后相同，不新增序列
        t.inc("n", labels=(("a", 1), ("b", 2)))
        t.inc("n", labels=(("b", 2), ("a", 1)))
        self.assertEqual(t.capacity()["used"], 6)

    def test_spans_do_not_consume_quota(self):
        t = Telemetry(max_series=1)
        t.inc("only-series")
        t.start("a"); t.finish("a")
        t.start("b", service="api", labels=(("k", "v"),))
        t.finish("b", service="api", error="boom")
        self.assertEqual(t.capacity(),
                         {"limit": 1, "used": 1, "remaining": 0})

    def test_capacity_is_independent_repeatable_and_does_not_read_clock(self):
        reads = []
        t = Telemetry(lambda: reads.append(1) or 0.0, max_series=2)
        t.inc("a")
        first = t.capacity()
        second = t.capacity()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["used"] = 99
        first["remaining"] = -1
        self.assertEqual(t.capacity(),
                         {"limit": 2, "used": 1, "remaining": 1})
        self.assertEqual(reads, [])  # capacity 不读 clock

    def test_capacity_not_in_snapshot_json_or_digest(self):
        t = Telemetry(max_series=2)
        t.inc("a"); t.observe("b", 1)
        snap = t.snapshot()
        self.assertEqual(set(snap), {"counters", "samples", "spans"})
        text = t.json()
        self.assertNotIn("limit", text)
        self.assertNotIn("used", text)
        self.assertNotIn("remaining", text)
        # 容量实例的摘要与无容量实例在相同数据下一致
        other = Telemetry()
        other.inc("a"); other.observe("b", 1)
        self.assertEqual(t.digest(), other.digest())

    # ------------------------------------------------------------------
    # inc/observe 容量门槛
    # ------------------------------------------------------------------

    def test_new_series_over_limit_raises_and_rolls_back(self):
        t = Telemetry(iter(range(100)).__next__, max_series=2)
        t.inc("c1")
        t.observe("s1", 1)
        # 已满：计数器与样本的新键都被拒绝
        for call in (lambda: t.inc("c2"),
                     lambda: t.observe("s2", 1)):
            with self.assertRaises(TelemetryCapacityError) as ctx:
                call()
            self.assertIsInstance(ctx.exception, ValueError)
        self.assertEqual(t.capacity(),
                         {"limit": 2, "used": 2, "remaining": 0})
        # 已有键继续可写
        t.inc("c1", 3)
        t.observe("s1", 2)
        self.assertEqual(t.counters[("", "c1", ())], 4)
        self.assertEqual(t.samples[("", "s1", ())], [1, 2])

    def test_zero_limit_rejects_all_new_series(self):
        t = Telemetry(max_series=0)
        with self.assertRaises(TelemetryCapacityError):
            t.inc("a")
        with self.assertRaises(TelemetryCapacityError):
            t.observe("a", 1)
        # 跨度不受容量限制
        t.start("s"); t.finish("s")
        self.assertEqual(t.capacity()["used"], 0)

    def test_rejection_before_clock_and_without_mutating_inputs(self):
        class AssertingClock:
            reads = 0
            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read")

        t = Telemetry(AssertingClock(), max_series=1)
        t.inc("a")
        labels = [("z", 9), ("a", 1)]
        saved = copy.deepcopy(labels)
        before = t.snapshot()
        with self.assertRaises(TelemetryCapacityError):
            t.inc("b", labels=labels)
        with self.assertRaises(TelemetryCapacityError):
            t.observe("b", 1, labels=labels)
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(labels, saved)  # 输入对象不被改写
        self.assertEqual(AssertingClock.reads, 0)

    def test_invalid_labels_valueerror_takes_precedence(self):
        # 输入校验先于容量检查：非法标签抛普通 ValueError，
        # 即使实例已满也不换成容量错误。
        t = Telemetry(max_series=0)
        with self.assertRaises(ValueError):
            t.inc("a", labels=(("k", float("nan")),))
        with self.assertRaises(ValueError):
            t.observe("a", 1, labels=(("k", object()),))
        # observe 的非有限值同样先于容量门槛
        with self.assertRaises(ValueError):
            t.observe("a", float("inf"))

    # ------------------------------------------------------------------
    # batch 容量检查与原子性
    # ------------------------------------------------------------------

    def test_batch_capacity_rejection_is_atomic_and_no_clock(self):
        t = Telemetry(iter(range(100)).__next__, max_series=2)
        t.inc("before")
        before = t.snapshot()

        class AssertingClock:
            reads = 0
            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read on rejection")

        t.clock = AssertingClock()
        # 第二个新序列即超限（已有 before + m 后 y 是第三个）
        events = [
            {"op": "observe", "name": "m", "value": 1},
            {"op": "inc", "name": "y"},
            {"op": "start", "span": "s"},
            {"op": "finish", "span": "s"},
        ]
        with self.assertRaises(TelemetryCapacityError):
            t.batch(events)
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.query("open"), [])
        self.assertEqual(AssertingClock.reads, 0)
        self.assertEqual(t.capacity()["used"], 1)

    def test_batch_existing_keys_at_capacity_succeed(self):
        t = Telemetry(iter(range(100)).__next__, max_series=2)
        t.inc("c")
        t.observe("s", 1)
        # 只触及已有序列的批次在满额时照常提交
        self.assertIsNone(t.batch([
            {"op": "inc", "name": "c", "value": 4},
            {"op": "observe", "name": "s", "value": 2},
            {"op": "start", "span": "sp"},
            {"op": "finish", "span": "sp"},
        ]))
        self.assertEqual(t.counters[("", "c", ())], 5)
        self.assertEqual(t.samples[("", "s", ())], [1, 2])

    def test_batch_validation_error_beats_capacity(self):
        # 事件本身非法时按批量契约抛普通 ValueError，不变成容量错误。
        t = Telemetry(max_series=0)
        with self.assertRaises(ValueError):
            t.batch([{"op": "inc", "name": "x", "service": ""}])
        with self.assertRaises(ValueError):
            t.batch([{"op": "observe", "name": "x"}])  # 缺 value

    def test_batch_within_limit_matches_sequential(self):
        events = [
            {"op": "inc", "name": "a"},
            {"op": "observe", "name": "b", "value": 1},
            {"op": "start", "span": "x"},
            {"op": "finish", "span": "x"},
        ]
        batched = Telemetry(iter(range(100)).__next__, max_series=5)
        batched.batch(copy.deepcopy(events))
        seq = Telemetry(iter(range(100)).__next__, max_series=5)
        for event in events:
            kwargs = {k: v for k, v in event.items() if k != "op"}
            getattr(seq, event["op"])(**kwargs)
        self.assertEqual(batched.snapshot(), seq.snapshot())

    # ------------------------------------------------------------------
    # 快照恢复/创建的容量
    # ------------------------------------------------------------------

    def _snapshot(self, counters=(), samples=(), spans=()):
        return {
            "counters": [
                {"service": svc, "name": name, "labels": list(labels),
                 "value": value}
                for svc, name, labels, value in counters
            ],
            "samples": [
                {"service": svc, "name": name, "labels": list(labels),
                 "values": list(values)}
                for svc, name, labels, values in samples
            ],
            "spans": list(spans),
        }

    def test_restore_and_from_snapshot_capacity(self):
        payload = self._snapshot(
            counters=[("", "c", (), 1), ("api", "c2", (), 2)],
            samples=[("", "s", (), [1, 2])],
        )
        for loader in (
            lambda: Telemetry.restore(payload, max_series=3),
            lambda: Telemetry.from_snapshot(payload, max_series=3),
        ):
            instance = loader()
            self.assertEqual(instance.capacity(),
                             {"limit": 3, "used": 3, "remaining": 0})
            # 已满：后续写入新序列被拒，已有序列可写
            with self.assertRaises(TelemetryCapacityError):
                instance.inc("new")
            instance.inc("c", 9)
            self.assertEqual(instance.counters[("", "c", ())], 10)
        with self.assertRaises(TelemetryCapacityError):
            Telemetry.restore(payload, max_series=2)
        with self.assertRaises(TelemetryCapacityError):
            Telemetry.from_snapshot(payload, max_series=0)
        # 省略 max_series 仍然不设限
        self.assertIsNone(Telemetry.restore(payload).capacity()["limit"])

    def test_restore_format_error_takes_precedence_over_capacity(self):
        # 格式问题一律 SnapshotFormatError，即使容量也放不下
        for bad in ("{not json", 42,
                    self._snapshot(counters=[("", "c", (), 1)],
                                   spans=[{"span": "s", "service": "",
                                           "parent": "ghost", "start": 0,
                                           "end": None, "error": None}])):
            with self.assertRaises(SnapshotFormatError, msg=repr(bad)):
                Telemetry.restore(bad, max_series=0)

    def test_capacity_parameter_invalid_before_payload_parsed(self):
        calls = []

        class ExplodingPayload(dict):
            def __deepcopy__(self, memo):
                calls.append(1)
                raise AssertionError("payload must not be touched")

        with self.assertRaises(ValueError):
            Telemetry.restore(ExplodingPayload(), max_series=True)
        with self.assertRaises(ValueError):
            Telemetry.from_snapshot(ExplodingPayload(), max_series=-2)
        self.assertEqual(calls, [])

    def test_resume_with_capacity_equals_full_recording(self):
        full = Telemetry(iter(range(100)).__next__)
        full.inc("a"); full.observe("b", 1)
        full.start("x"); full.finish("x")
        full.inc("c")

        partial = self._snapshot(
            counters=[("", "a", (), 1)],
            samples=[("", "b", (), [1])],
            spans=[{"span": "x", "service": "", "parent": None,
                    "start": 0, "end": 1, "error": None}],
        )
        resumed = Telemetry.restore(
            partial, clock=iter(range(2, 100)).__next__, max_series=10
        )
        resumed.inc("c")
        self.assertEqual(resumed.snapshot(), full.snapshot())
        self.assertEqual(resumed.json(), full.json())
        self.assertEqual(resumed.digest(), full.digest())

    # ------------------------------------------------------------------
    # restore_snapshot 沿用实例上限
    # ------------------------------------------------------------------

    def test_restore_snapshot_keeps_instance_limit(self):
        t = Telemetry(max_series=1)
        t.inc("keep")
        before = t.snapshot()
        oversized = self._snapshot(
            counters=[("", "a", (), 1), ("", "b", (), 2)],
        )
        with self.assertRaises(TelemetryCapacityError):
            t.restore_snapshot(oversized)
        # 聚合、时钟配置与容量上限保持调用前状态
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.max_series, 1)
        # 空快照恢复后仍沿用该上限
        self.assertIsNone(
            t.restore_snapshot({"counters": [], "samples": [], "spans": []})
        )
        self.assertEqual(t.capacity(),
                         {"limit": 1, "used": 0, "remaining": 1})
        t.inc("a")
        with self.assertRaises(TelemetryCapacityError):
            t.inc("b")

    # ------------------------------------------------------------------
    # merge_snapshot / merge_snapshots
    # ------------------------------------------------------------------

    def test_merge_snapshot_capacity_atomic(self):
        t = Telemetry(max_series=3)
        t.inc("local")
        incoming = self._snapshot(
            counters=[("", "a", (), 1), ("", "b", (), 2)],
            samples=[("", "s", (), [1])],
        )
        before = t.snapshot()
        with self.assertRaises(TelemetryCapacityError):
            t.merge_snapshot(incoming)  # 1 + 3 = 4 > 3
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.capacity()["used"], 1)
        # 只含重叠键（不产生新序列）的分片在满额时可合并
        full = self._snapshot(counters=[("", "a", (), 5)])
        t2 = Telemetry(max_series=1)
        t2.inc("a", 1)
        t2.merge_snapshot(full)
        self.assertEqual(t2.counters[("", "a", ())], 6)
        self.assertEqual(t2.capacity()["remaining"], 0)

    def test_merge_snapshots_capacity_atomic(self):
        t = Telemetry(max_series=3)
        t.inc("local")
        shard_a = self._snapshot(counters=[("", "a", (), 1)])
        shard_b = self._snapshot(
            counters=[("", "b", (), 1)],
            samples=[("", "s", (), [1])],
        )
        before = t.snapshot()
        with self.assertRaises(TelemetryCapacityError):
            t.merge_snapshots([shard_a, shard_b])  # 合计 1+1+2=4 > 3
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.capacity()["used"], 1)
        # 空集合仍然成功且不动状态
        self.assertIsNone(t.merge_snapshots([]))
        self.assertEqual(t.snapshot(), before)

    def test_merge_format_and_conflict_errors_preserved(self):
        t = Telemetry(max_series=0)
        # 格式错误仍是 SnapshotFormatError（先于容量检查）
        with self.assertRaises(SnapshotFormatError):
            t.merge_snapshot("{bad")
        with self.assertRaises(SnapshotFormatError):
            t.merge_snapshots(["{bad"])
        # 同键跨度冲突仍是普通 ValueError，整体回滚
        base = Telemetry(iter(range(10)).__next__)
        base.start("x"); base.finish("x")
        conflicting = copy.deepcopy(base.snapshot())
        conflicting["spans"][0]["end"] = 999
        before = t.snapshot()
        with self.assertRaises(ValueError):
            t.merge_snapshots([base.snapshot(), conflicting])
        self.assertEqual(t.snapshot(), before)

    def test_merge_success_then_writes_match_full_recording(self):
        shard1 = self._snapshot(counters=[("", "a", (), 1)])
        shard2 = self._snapshot(samples=[("", "b", (), [5])])
        merged = Telemetry(iter(range(100)).__next__, max_series=5)
        merged.merge_snapshots([shard1, shard2])
        merged.inc("a", 2)
        merged.observe("b", 6)

        full = Telemetry(iter(range(100)).__next__, max_series=5)
        full.inc("a")
        full.observe("b", 5)
        full.inc("a", 2)
        full.observe("b", 6)
        self.assertEqual(merged.snapshot(), full.snapshot())

    # ------------------------------------------------------------------
    # 其他只读入口行为不变
    # ------------------------------------------------------------------

    def test_readonly_entries_unaffected_by_capacity(self):
        t = Telemetry(iter(range(100)).__next__, max_series=2)
        t.inc("a", labels=(("k", "v"),))
        t.observe("lat", 10)
        t.observe("lat", 20)
        t.start("root"); t.start("child", parent="root")
        t.finish("child", error="boom"); t.finish("root")

        self.assertEqual(t.query("error")[0]["span"], "child")
        self.assertEqual(t.trace("root")["span"], "root")
        self.assertEqual(len(t.trace("root")["children"]), 1)
        self.assertEqual(t.percentile("lat", 50), 15.0)
        self.assertEqual(
            t.histogram("lat", [15])["counts"], [1, 1]
        )
        stats = t.span_duration_stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration(minimum=0)],
            ["root", "child"],
        )
        # 满额后只读入口照常工作，digest 仍稳定
        snap_digest = t.digest()
        with self.assertRaises(TelemetryCapacityError):
            t.inc("overflow")
        self.assertEqual(t.digest(), snap_digest)


class TelemetryCounterSummaryTest(unittest.TestCase):
    def _build(self):
        t = Telemetry()
        # 默认服务、无标签序列：1 + 2 = 3
        t.inc("hits")
        t.inc("hits", 2)
        # 默认服务、带标签序列：4
        t.inc("hits", 4, labels=(("k", "v"),))
        # api 服务、无标签序列：8 + 16 = 24
        t.inc("hits", 8, service="api")
        t.inc("hits", 16, service="api")
        # 其他名称不应混入
        t.inc("other", 100)
        t.inc("other", 200, service="api")
        return t

    def test_sums_all_same_name_series_in_snapshot_order(self):
        t = self._build()
        summary = t.counter_summary("hits")
        # 期望值按快照稳定顺序（服务、名称、标签）逐序列取 value 原生相加
        ordered_values = [
            record["value"]
            for record in t.snapshot()["counters"]
            if record["name"] == "hits"
        ]
        self.assertEqual(ordered_values, [3, 4, 24])
        total = 0
        for value in ordered_values:
            total = total + value
        self.assertEqual(
            summary,
            {"series_count": 3, "value": total},
        )
        self.assertEqual(summary, {"series_count": 3, "value": 31})
        self.assertIsInstance(summary["series_count"], int)
        self.assertIsInstance(summary["value"], int)
        self.assertEqual(set(summary), {"series_count", "value"})

    def test_selection_order_is_snapshot_order_not_insertion_order(self):
        # 逆快照顺序写入，选择顺序仍必须按服务、名称、标签排列
        t = Telemetry()
        t.inc("hits", 24, service="api")
        t.inc("hits", 4, labels=(("k", "v"),))
        t.inc("hits", 3)
        # 浮点逐位验证：另构造一组跨服务浮点计数器，顺序必须与快照一致
        f = Telemetry()
        f.inc("rate", 0.1, service="b")
        f.inc("rate", 0.2, service="b", labels=(("k", "v"),))
        f.inc("rate", 0.3)  # 默认服务排在 b 之前
        ordered = [
            record["value"]
            for record in f.snapshot()["counters"]
            if record["name"] == "rate"
        ]
        self.assertEqual(ordered, [0.3, 0.1, 0.2])
        expected = 0
        for value in ordered:
            expected = expected + value
        summary = f.counter_summary("rate")
        self.assertEqual(summary["series_count"], 3)
        self.assertEqual(summary["value"].hex(), expected.hex())
        self.assertEqual(t.counter_summary("hits")["value"], 31)

    def test_service_filter_none_empty_and_named(self):
        t = self._build()
        all_summary = t.counter_summary("hits", service=None)
        self.assertEqual(all_summary["series_count"], 3)
        self.assertEqual(all_summary["value"], 31)
        # 省略 service 与显式 None 等价
        self.assertEqual(t.counter_summary("hits"), all_summary)
        # 空字符串精确匹配默认服务：无标签与带标签两个序列
        default_summary = t.counter_summary("hits", service="")
        self.assertEqual(default_summary, {"series_count": 2, "value": 7})
        # 具名服务精确匹配
        self.assertEqual(
            t.counter_summary("hits", service="api"),
            {"series_count": 1, "value": 24},
        )
        self.assertIsNone(t.counter_summary("hits", service="web"))
        for bad in (1, 1.5, b"api", ["api"], object()):
            with self.assertRaises(ValueError):
                t.counter_summary("hits", service=bad)

    def test_labels_filter_none_empty_and_specific(self):
        t = self._build()
        # None 匹配全部标签集合
        self.assertEqual(t.counter_summary("hits", labels=None)["value"], 31)
        # 显式空标签只匹配无标签计数器：默认服务与 api 各一条
        self.assertEqual(
            t.counter_summary("hits", labels=()),
            {"series_count": 2, "value": 27},
        )
        # 具名标签按归一化后的完整集合精确匹配
        self.assertEqual(
            t.counter_summary("hits", labels=(("k", "v"),)),
            {"series_count": 1, "value": 4},
        )
        # 标签输入顺序不影响匹配
        t.inc("hits2", 3, labels=(("a", 1), ("b", 2)))
        self.assertEqual(
            t.counter_summary("hits2", labels=(("b", 2), ("a", 1))),
            {"series_count": 1, "value": 3},
        )
        # 数组/对象标签值按规范化后的完整 JSON 结构精确匹配
        t.inc("hits3", 9, labels=(("k", ["a", "b"]),))
        self.assertEqual(
            t.counter_summary("hits3", labels=(("k", ["a", "b"]),))["value"],
            9,
        )
        # 没有该标签集合时返回 None
        self.assertIsNone(t.counter_summary("hits", labels=(("k", "x"),)))
        # 重复键、不可严格 JSON 序列化的值统一 ValueError
        with self.assertRaises(ValueError):
            t.counter_summary("hits", labels=(("k", 1), ("k", 2)))
        with self.assertRaises(ValueError):
            t.counter_summary("hits", labels=(("k", float("nan")),))
        with self.assertRaises(ValueError):
            t.counter_summary("hits", labels=(("k", object()),))

    def test_service_and_labels_filter_combined(self):
        t = self._build()
        self.assertEqual(
            t.counter_summary("hits", labels=(), service="api"),
            {"series_count": 1, "value": 24},
        )
        self.assertIsNone(
            t.counter_summary("hits", labels=(("k", "v"),), service="api")
        )
        self.assertIsNone(t.counter_summary("hits", labels=(), service="web"))

    def test_only_exact_name_matches(self):
        t = self._build()
        self.assertIsNone(t.counter_summary("hit"))
        self.assertIsNone(t.counter_summary("hitsx"))
        self.assertEqual(t.counter_summary("other")["value"], 300)

    def test_missing_name_and_empty_aggregator_return_none(self):
        t = self._build()
        self.assertIsNone(t.counter_summary("nope"))
        # 空聚合器
        self.assertIsNone(Telemetry().counter_summary("hits"))
        # 服务/标签筛选后无同名序列同样返回 None
        self.assertIsNone(t.counter_summary("other", service="web"))

    def test_unhashable_name_rejected_before_reading_counters(self):
        t = self._build()
        with self.assertRaises(ValueError):
            t.counter_summary(["hits"])
        with self.assertRaises(ValueError):
            t.counter_summary({"a": 1})
        with self.assertRaises(ValueError):
            t.counter_summary({"a"})

    def test_native_addition_types_preserved_without_float_coercion(self):
        # 纯整数：自整数 0 相加，结果仍是 int
        t = Telemetry()
        t.inc("n", 1)
        t.inc("n", 2)
        result = t.counter_summary("n")
        self.assertEqual(result, {"series_count": 1, "value": 3})
        self.assertIs(type(result["value"]), int)

        # int 与 float 以 Python 加法语义相遇后才成为 float，不做隐式整体转换
        mixed = Telemetry()
        mixed.inc("n", 5, service="a")
        mixed.inc("n", 2.5, service="b")
        result = mixed.counter_summary("n")
        self.assertEqual(result["value"], 7.5)
        self.assertIs(type(result["value"]), float)

        # Decimal 与 Fraction 的原生类型在各自同类型序列中保留
        dec = Telemetry()
        dec.inc("d", Decimal("0.1"))
        dec.inc("d", Decimal("0.2"))
        result = dec.counter_summary("d")
        self.assertIsInstance(result["value"], Decimal)
        self.assertEqual(result["value"], Decimal("0.3"))

        frac = Telemetry()
        frac.inc("f", Fraction(1, 3))
        frac.inc("f", Fraction(1, 6))
        result = frac.counter_summary("f")
        self.assertIsInstance(result["value"], Fraction)
        self.assertEqual(result["value"], Fraction(1, 2))

        # bool 按 int 加法语义参与：0 + True 得到 int 1，而非 1.0 或 True
        booleans = Telemetry()
        booleans.counters[("", "b", ())] = True
        result = booleans.counter_summary("b")
        self.assertEqual(result["value"], 1)
        self.assertIs(type(result["value"]), int)
        self.assertIsNot(type(result["value"]), bool)

    def test_addition_failure_raises_value_error_without_partial_result(self):
        # 与整数 0 无法相加的值（字符串）：统一 ValueError
        t = Telemetry()
        t.inc("n", 1, service="a")
        t.counters[("b", "n", ())] = "oops"
        with self.assertRaises(ValueError):
            t.counter_summary("n")

        # 自定义 __radd__ 抛任意异常同样归一为 ValueError
        class ExplodingValue:
            __slots__ = ()

            def __radd__(self, other):
                raise RuntimeError("boom")

        t.counters[("b", "n", ())] = ExplodingValue()
        with self.assertRaises(ValueError):
            t.counter_summary("n")

        # 首个序列就抛异常（0 + value 走 __add__）也归一为 ValueError
        class ExplodingFirst:
            def __add__(self, other):
                raise RuntimeError("bang")

        first = Telemetry()
        first.counters[("", "n", ())] = ExplodingFirst()
        with self.assertRaises(ValueError):
            first.counter_summary("n")

    def test_invalid_arguments_rejected_before_reading_counters(self):
        t = Telemetry()
        t.inc("n", 1)
        t.counters[("b", "n", ())] = "bad"  # 历史值本会导致相加失败
        # 坏 service 与坏数据并存时先抛参数 ValueError，不进入相加
        with self.assertRaises(ValueError):
            t.counter_summary("n", service=123)
        # 坏 labels 同样先于计数器读取
        with self.assertRaises(ValueError):
            t.counter_summary("n", labels=(("k", 1), ("k", 2)))
        with self.assertRaises(ValueError):
            t.counter_summary(["n"], service="api")

    def test_does_not_read_clock_or_mutate_state(self):
        class BoomClock:
            def __call__(self):
                raise AssertionError("clock must not be called")

        t = Telemetry(BoomClock())
        t.inc("hits", 3)
        t.inc("hits", 4, service="api")
        before = t.snapshot()
        before_digest = t.digest()
        summary = t.counter_summary("hits")
        self.assertEqual(summary, {"series_count": 2, "value": 7})
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.digest(), before_digest)
        # 输入标签不被修改
        labels = [("b", 2), ("a", 1)]
        t.inc("tagged", 5, labels=labels)
        t.counter_summary("tagged", labels=labels)
        self.assertEqual(labels, [("b", 2), ("a", 1)])

    def test_result_is_independent_and_repeatable(self):
        t = self._build()
        first = t.counter_summary("hits")
        second = t.counter_summary("hits")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["value"] = 999
        first["extra"] = "x"
        third = t.counter_summary("hits")
        self.assertEqual(third["value"], 31)
        self.assertNotIn("extra", third)

    def test_works_after_restore_merge_and_batch(self):
        t = self._build()
        restored = Telemetry.restore(t.json())
        self.assertEqual(
            restored.counter_summary("hits"), t.counter_summary("hits")
        )
        # 合并进来的计数器同样可查询，相同键按加法语义累加
        merged = Telemetry()
        merged.inc("hits", 100)
        merged.merge_snapshot(t.snapshot(service=""))
        self.assertEqual(
            merged.counter_summary("hits"),
            {"series_count": 2, "value": 107},
        )
        # batch 回放产生的计数器可查询
        batched = Telemetry()
        batched.batch([
            {"op": "inc", "name": "x", "value": 5},
            {"op": "inc", "name": "x", "value": 7, "service": "s"},
        ])
        self.assertEqual(
            batched.counter_summary("x"),
            {"series_count": 2, "value": 12},
        )
        # from_snapshot 路径同样可查询
        rebuilt = Telemetry.from_snapshot(t.snapshot())
        self.assertEqual(
            rebuilt.counter_summary("hits"), t.counter_summary("hits")
        )

    def test_does_not_add_snapshot_fields_or_change_digest(self):
        t = self._build()
        t.counter_summary("hits")
        t.counter_summary("hits", service="api", labels=())
        for record in t.snapshot()["counters"]:
            self.assertEqual(
                set(record), {"service", "name", "labels", "value"}
            )
        payload = json.loads(t.json())
        for record in payload["counters"]:
            self.assertEqual(
                set(record), {"service", "name", "labels", "value"}
            )
        # 摘要结果不出现在任何快照/diff 字段中
        diff = Telemetry.diff_snapshots(Telemetry().snapshot(), t.snapshot())
        self.assertEqual(set(diff), {"counters", "samples", "spans"})


class TelemetrySampleSummaryTest(unittest.TestCase):
    def _build(self):
        t = Telemetry()
        # 默认服务、无标签序列：2 个值
        t.observe("lat", 1)
        t.observe("lat", 2)
        # 默认服务、带标签序列：2 个值
        t.observe("lat", 4, labels=(("k", "v"),))
        t.observe("lat", 8, labels=(("k", "v"),))
        # api 服务、无标签序列：3 个值
        t.observe("lat", 16, service="api")
        t.observe("lat", 32, service="api")
        t.observe("lat", 64, service="api")
        # 其他名称不应混入
        t.observe("other", 100)
        t.observe("other", 200, service="api")
        return t

    def test_aggregates_all_same_name_series_in_snapshot_order(self):
        t = self._build()
        summary = t.sample_summary("lat")
        # 期望值按快照稳定顺序（服务、名称、标签）逐序列、序列内按写入顺序合并
        ordered_values = []
        for record in t.snapshot()["samples"]:
            if record["name"] == "lat":
                ordered_values.extend(record["values"])
        self.assertEqual(len(ordered_values), 7)
        total = 0.0
        for value in ordered_values:
            total += value
        self.assertEqual(
            summary,
            {
                "series_count": 3,
                "count": 7,
                "sum": total,
                "minimum": 1.0,
                "maximum": 64.0,
                "mean": total / 7,
            },
        )
        self.assertIsInstance(summary["series_count"], int)
        self.assertIsInstance(summary["count"], int)
        for key in ("sum", "minimum", "maximum", "mean"):
            self.assertIsInstance(summary[key], float)
        self.assertEqual(set(summary), {
            "series_count", "count", "sum", "minimum", "maximum", "mean"
        })

    def test_service_filter_none_empty_and_named(self):
        t = self._build()
        all_summary = t.sample_summary("lat", service=None)
        self.assertEqual(all_summary["series_count"], 3)
        self.assertEqual(all_summary["count"], 7)
        # 省略 service 与显式 None 等价
        self.assertEqual(t.sample_summary("lat"), all_summary)
        # 空字符串精确匹配默认服务：无标签与带标签两个序列
        default_summary = t.sample_summary("lat", service="")
        self.assertEqual(default_summary["series_count"], 2)
        self.assertEqual(default_summary["count"], 4)
        self.assertEqual(default_summary["minimum"], 1.0)
        self.assertEqual(default_summary["maximum"], 8.0)
        # 具名服务精确匹配
        api_summary = t.sample_summary("lat", service="api")
        self.assertEqual(api_summary["series_count"], 1)
        self.assertEqual(api_summary["count"], 3)
        self.assertEqual(api_summary["sum"], 112.0)
        self.assertEqual(api_summary["minimum"], 16.0)
        self.assertEqual(api_summary["maximum"], 64.0)
        self.assertIsNone(t.sample_summary("lat", service="web"))
        for bad in (1, 1.5, b"api", ["api"], object()):
            with self.assertRaises(ValueError):
                t.sample_summary("lat", service=bad)

    def test_labels_filter_none_empty_and_specific(self):
        t = self._build()
        # None 匹配全部标签集合
        self.assertEqual(t.sample_summary("lat", labels=None)["count"], 7)
        # 显式空序列只匹配无标签样本：默认服务与 api 各一条
        unlabeled = t.sample_summary("lat", labels=())
        self.assertEqual(unlabeled["series_count"], 2)
        self.assertEqual(unlabeled["count"], 5)
        self.assertEqual(unlabeled["minimum"], 1.0)
        self.assertEqual(unlabeled["maximum"], 64.0)
        # 具名标签按归一化后的完整集合精确匹配
        tagged = t.sample_summary("lat", labels=(("k", "v"),))
        self.assertEqual(tagged["series_count"], 1)
        self.assertEqual(tagged["count"], 2)
        self.assertEqual(tagged["sum"], 12.0)
        # 标签输入顺序不影响匹配
        t.observe("lat2", 3, labels=(("a", 1), ("b", 2)))
        self.assertEqual(
            t.sample_summary("lat2", labels=(("b", 2), ("a", 1)))["count"], 1
        )
        # 没有该标签集合时返回 None
        self.assertIsNone(t.sample_summary("lat", labels=(("k", "x"),)))
        # 重复键、不可严格 JSON 序列化的值统一 ValueError
        with self.assertRaises(ValueError):
            t.sample_summary("lat", labels=(("k", 1), ("k", 2)))
        with self.assertRaises(ValueError):
            t.sample_summary("lat", labels=(("k", float("nan")),))

    def test_service_and_labels_filter_combined(self):
        t = self._build()
        summary = t.sample_summary(
            "lat", labels=(), service="api"
        )
        self.assertEqual(summary["series_count"], 1)
        self.assertEqual(summary["count"], 3)
        self.assertIsNone(t.sample_summary("lat", labels=(("k", "v"),), service="api"))
        self.assertIsNone(t.sample_summary("lat", labels=(), service="web"))

    def test_missing_name_and_empty_series_return_none(self):
        t = self._build()
        self.assertIsNone(t.sample_summary("nope"))
        # 空聚合器
        self.assertIsNone(Telemetry().sample_summary("lat"))
        # 同名序列存在但全部为空（max_values_per_series=0）不参与汇总
        trimmed = Telemetry(max_values_per_series=0)
        trimmed.observe("lat", 1)
        trimmed.observe("lat", 2, service="api")
        self.assertEqual(trimmed.sample_summary("lat"), None)
        # 一条非空加一条空：只统计非空序列
        mixed = Telemetry(max_values_per_series=1)
        mixed.observe("lat", 1)
        mixed.observe("lat", 2)  # 窗口只保留 [2]
        mixed.samples[("api", "lat", ())] = []  # 直接构造空序列
        summary = mixed.sample_summary("lat")
        self.assertEqual(summary["series_count"], 1)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["sum"], 2.0)

    def test_unhashable_name_rejected_before_reading_samples(self):
        t = self._build()
        with self.assertRaises(ValueError):
            t.sample_summary(["lat"])
        with self.assertRaises(ValueError):
            t.sample_summary({"a": 1})

    def test_validation_before_read_and_no_partial_result(self):
        # 不可转换的历史值：统一 ValueError，不返回部分字典
        t = Telemetry()
        t.observe("lat", 1)
        t.observe("lat", 2, service="api")
        t.samples[("web", "lat", ())] = [4, object()]
        with self.assertRaises(ValueError):
            t.sample_summary("lat")
        # 超大整数溢出同样归一为 ValueError
        t.samples[("web", "lat", ())] = [10 ** 400]
        with self.assertRaises(ValueError):
            t.sample_summary("lat")
        # 自定义 __float__ 抛异常也统一为 ValueError
        class ExplodingValue:
            def __float__(self):
                raise RuntimeError("boom")

        t.samples[("web", "lat", ())] = [ExplodingValue()]
        with self.assertRaises(ValueError):
            t.sample_summary("lat")
        # 非法参数先于历史值转换：坏 service + 坏数据并存时仍是参数 ValueError
        with self.assertRaises(ValueError):
            t.sample_summary("lat", service=123)

    def test_does_not_read_clock_or_mutate_state(self):
        class BoomClock:
            def __call__(self):
                raise AssertionError("clock must not be called")

        t = Telemetry(BoomClock())
        t.observe("lat", 1)
        t.observe("lat", 2, service="api")
        before = t.snapshot()
        before_digest = t.digest()
        summary = t.sample_summary("lat")
        self.assertEqual(summary["count"], 2)
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.digest(), before_digest)
        # 输入标签不被修改
        labels = [("b", 2), ("a", 1)]
        t.observe("tagged", 5, labels=labels)
        t.sample_summary("tagged", labels=labels)
        self.assertEqual(labels, [("b", 2), ("a", 1)])

    def test_result_is_independent_and_repeatable(self):
        t = self._build()
        first = t.sample_summary("lat")
        second = t.sample_summary("lat")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["count"] = 999
        first["extra"] = "x"
        third = t.sample_summary("lat")
        self.assertEqual(third["count"], 7)
        self.assertNotIn("extra", third)

    def test_sum_order_is_snapshot_order_then_write_order(self):
        # 两个序列的值各自按写入顺序；序列之间按快照（服务、名称、标签）顺序
        t = Telemetry()
        t.observe("lat", 0.1, service="b")
        t.observe("lat", 0.2, service="b")
        t.observe("lat", 0.3)  # 默认服务排在 b 之前
        ordered = [0.3, 0.1, 0.2]
        expected_sum = 0.0
        for value in ordered:
            expected_sum += value
        summary = t.sample_summary("lat")
        self.assertEqual(summary["series_count"], 2)
        # 逐位相同：累加顺序必须确定
        self.assertEqual(summary["sum"].hex(), expected_sum.hex())
        self.assertEqual(summary["mean"], expected_sum / 3)

    def test_works_after_restore_and_does_not_change_snapshot_fields(self):
        t = self._build()
        payload = t.json()
        restored = Telemetry.restore(payload)
        self.assertEqual(
            restored.sample_summary("lat"), t.sample_summary("lat")
        )
        # 快照样本条目字段形状不新增任何内容
        for record in restored.snapshot()["samples"]:
            self.assertNotIn("series_count", record)
        self.assertEqual(restored.digest(), t.digest())


class TelemetryHistogramSummaryTest(unittest.TestCase):
    def _build(self):
        t = Telemetry()
        # 默认服务、无标签序列：0, 1
        t.observe("x", 0)
        t.observe("x", 1)
        # 默认服务、带标签序列：2, 2, 5
        t.observe("x", 2, labels=(("k", "v"),))
        t.observe("x", 2, labels=(("k", "v"),))
        t.observe("x", 5, labels=(("k", "v"),))
        # api 服务、无标签序列：9, 10, 10.5, 11
        for value in (9, 10, 10.5, 11):
            t.observe("x", value, service="api")
        # 其他名称不应混入
        t.observe("other", 100)
        t.observe("other", 200, service="api")
        return t

    def test_merges_same_name_series_in_snapshot_order(self):
        t = self._build()
        result = t.histogram_summary("x", [1, 5, 10])
        # 拼接顺序与快照中同名序列的 values 顺序逐项一致
        ordered_values = []
        for record in t.snapshot()["samples"]:
            if record["name"] == "x":
                ordered_values.extend(record["values"])
        self.assertEqual(
            ordered_values, [0, 1, 2, 2, 5, 9, 10, 10.5, 11]
        )
        # 左开右闭：<=1: 0,1 ｜ (1,5]: 2,2,5 ｜ (5,10]: 9,10 ｜ >10: 10.5,11
        self.assertEqual(
            result,
            {"boundaries": [1, 5, 10], "counts": [2, 3, 2, 2], "count": 9},
        )
        self.assertIsInstance(result["count"], int)
        self.assertTrue(all(isinstance(c, int) for c in result["counts"]))
        self.assertEqual(
            set(result), {"boundaries", "counts", "count"}
        )
        # tuple 边界同样合法
        self.assertEqual(
            t.histogram_summary("x", (1, 5, 10)), result
        )

    def test_service_filter_none_empty_and_named(self):
        t = self._build()
        all_result = t.histogram_summary("x", [1, 5, 10], service=None)
        self.assertEqual(all_result["counts"], [2, 3, 2, 2])
        # 省略 service 与显式 None 等价
        self.assertEqual(t.histogram_summary("x", [1, 5, 10]), all_result)
        # 空字符串精确匹配默认服务（含带标签序列）
        self.assertEqual(
            t.histogram_summary("x", [1, 5, 10], service="")["counts"],
            [2, 3, 0, 0],
        )
        # 具名服务精确匹配
        self.assertEqual(
            t.histogram_summary("x", [1, 5, 10], service="api")["counts"],
            [0, 0, 2, 2],
        )
        self.assertIsNone(t.histogram_summary("x", [1], service="web"))
        for bad in (1, 1.5, b"api", ["api"], object()):
            with self.assertRaises(ValueError):
                t.histogram_summary("x", [1], service=bad)

    def test_labels_filter_none_empty_and_specific(self):
        t = self._build()
        self.assertEqual(
            t.histogram_summary("x", [1, 5, 10], labels=None)["count"], 9
        )
        # 显式空标签只命中无标签序列（默认服务与 api 各一条）
        self.assertEqual(
            t.histogram_summary("x", [1, 5, 10], labels=())["counts"],
            [2, 0, 2, 2],
        )
        # 具名标签按归一化完整集合精确匹配
        self.assertEqual(
            t.histogram_summary(
                "x", [1, 5, 10], labels=(("k", "v"),)
            )["counts"],
            [0, 3, 0, 0],
        )
        # 标签输入顺序不影响匹配
        t.observe("y", 3, labels=(("a", 1), ("b", 2)))
        self.assertEqual(
            t.histogram_summary(
                "y", [0, 5], labels=(("b", 2), ("a", 1))
            )["counts"],
            [0, 1, 0],
        )
        # 没有该标签集合时返回 None
        self.assertIsNone(
            t.histogram_summary("x", [1], labels=(("k", "x"),))
        )
        # 重复键、不可严格 JSON 序列化的值统一 ValueError
        with self.assertRaises(ValueError):
            t.histogram_summary("x", [1], labels=(("k", 1), ("k", 2)))
        with self.assertRaises(ValueError):
            t.histogram_summary("x", [1], labels=(("k", float("nan")),))

    def test_service_and_labels_filter_combined(self):
        t = self._build()
        result = t.histogram_summary("x", [1, 5, 10], labels=(), service="api")
        self.assertEqual(result["counts"], [0, 0, 2, 2])
        self.assertIsNone(
            t.histogram_summary(
                "x", [1, 5, 10], labels=(("k", "v"),), service="api"
            )
        )
        self.assertIsNone(
            t.histogram_summary("x", [1], labels=(), service="web")
        )

    def test_missing_name_and_empty_series_return_none(self):
        t = self._build()
        self.assertIsNone(t.histogram_summary("nope", [1]))
        self.assertIsNone(Telemetry().histogram_summary("x", [1]))
        # 同名序列存在但 values 全为空时不参与分桶
        payload = {
            "counters": [],
            "samples": [
                {"service": "", "name": "x", "labels": [], "values": []},
                {
                    "service": "api",
                    "name": "x",
                    "labels": [],
                    "values": [],
                },
            ],
            "spans": [],
        }
        self.assertIsNone(
            Telemetry.from_snapshot(payload).histogram_summary("x", [1])
        )
        # 一条空、一条非空：只统计非空序列
        payload["samples"][1]["values"] = [7]
        result = Telemetry.from_snapshot(payload).histogram_summary("x", [5])
        self.assertEqual(
            result, {"boundaries": [5], "counts": [0, 1], "count": 1}
        )

    def test_counters_are_not_included(self):
        t = Telemetry()
        t.inc("x", 5)
        t.inc("x", 5)
        # 同名计数器不构成样本序列
        self.assertIsNone(t.histogram_summary("x", [1]))

    def test_unhashable_name_rejected_before_reading_samples(self):
        t = self._build()
        with self.assertRaises(ValueError):
            t.histogram_summary(["x"], [1])
        with self.assertRaises(ValueError):
            t.histogram_summary({"a": 1}, [1])
        # name 不可哈希时即使边界也非法，仍先抛 ValueError
        with self.assertRaises(ValueError):
            t.histogram_summary({}, [1, True])

    def test_boundaries_validated_before_reading_samples(self):
        t = self._build()
        bad_boundaries = (
            [],                       # 空列表
            (),                       # 空元组
            [5, 1],                   # 非递增
            [1, 1],                   # 非严格递增
            [1, True],                # bool
            [False],                  # bool
            [1, float("nan")],        # NaN
            [1, float("inf")],        # 无穷
            [float("-inf"), 1],       # 负无穷
            [1, "2"],                 # 字符串
            [1, 2j],                  # 复数
            (0.0, -0.0),              # 0.0 与 -0.0 不严格递增
            "abc",                    # 非 list/tuple
            5,                        # 标量
        )
        for boundaries in bad_boundaries:
            with self.assertRaises(ValueError):
                t.histogram_summary("x", boundaries)

    def test_validation_before_read_and_no_partial_result(self):
        t = Telemetry()
        t.observe("lat", 1)
        t.observe("lat", 2, service="api")
        # 不可转换的历史值：统一 ValueError，不返回部分结果
        t.samples[("web", "lat", ())] = [4, object()]
        with self.assertRaises(ValueError):
            t.histogram_summary("lat", [1])
        # 超大整数溢出同样归一为 ValueError
        t.samples[("web", "lat", ())] = [10 ** 400]
        with self.assertRaises(ValueError):
            t.histogram_summary("lat", [1])
        # NaN / 无穷历史值
        t.samples[("web", "lat", ())] = [float("nan")]
        with self.assertRaises(ValueError):
            t.histogram_summary("lat", [1])
        t.samples[("web", "lat", ())] = [float("inf")]
        with self.assertRaises(ValueError):
            t.histogram_summary("lat", [1])
        # 自定义 __float__ 抛异常也统一为 ValueError
        class ExplodingValue:
            def __float__(self):
                raise RuntimeError("boom")

        t.samples[("web", "lat", ())] = [ExplodingValue()]
        with self.assertRaises(ValueError):
            t.histogram_summary("lat", [1])
        # 非法参数先于历史值转换：坏 service 与坏数据并存时先抛参数 ValueError
        with self.assertRaises(ValueError):
            t.histogram_summary("lat", [1], service=123)

    def test_bucketing_is_left_open_right_closed(self):
        t = Telemetry()
        for value in (1, 2, 3):
            t.observe("z", value)
        # 恰好在边界上的值落入左侧边界对应桶（<=）
        self.assertEqual(
            t.histogram_summary("z", [1, 2, 3])["counts"], [1, 1, 1, 0]
        )
        t2 = Telemetry()
        for value in (-2.0, -1, 0, 1.5):
            t2.observe("z", value)
        self.assertEqual(
            t2.histogram_summary("z", [-1, 0])["counts"], [2, 1, 1]
        )
        # 单个边界只有两个桶
        t3 = Telemetry()
        t3.observe("s", 1)
        t3.observe("s", 2)
        self.assertEqual(
            t3.histogram_summary("s", [1]),
            {"boundaries": [1], "counts": [1, 1], "count": 2},
        )

    def test_does_not_read_clock_or_mutate_state(self):
        class BoomClock:
            def __call__(self):
                raise AssertionError("clock must not be called")

        t = Telemetry(BoomClock())
        t.observe("lat", 1)
        t.observe("lat", 2, service="api")
        before = t.snapshot()
        before_json = t.json()
        before_digest = t.digest()
        result = t.histogram_summary("lat", [1])
        self.assertEqual(result["count"], 2)
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.json(), before_json)
        self.assertEqual(t.digest(), before_digest)
        # 输入标签与边界不被修改
        labels = [("b", 2), ("a", 1)]
        t.observe("tagged", 5, labels=labels)
        t.histogram_summary("tagged", [1, 5], labels=labels)
        self.assertEqual(labels, [("b", 2), ("a", 1)])
        boundaries = [1, 5]
        t.histogram_summary("tagged", boundaries)
        self.assertEqual(boundaries, [1, 5])

    def test_result_is_independent_and_repeatable(self):
        t = self._build()
        first = t.histogram_summary("x", [1, 5, 10])
        second = t.histogram_summary("x", [1, 5, 10])
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["boundaries"], second["boundaries"])
        self.assertIsNot(first["counts"], second["counts"])
        first["boundaries"].append(999)
        first["counts"].append(999)
        first["count"] = 0
        first["extra"] = "x"
        third = t.histogram_summary("x", [1, 5, 10])
        self.assertEqual(
            third,
            {"boundaries": [1, 5, 10], "counts": [2, 3, 2, 2], "count": 9},
        )
        self.assertNotIn("extra", third)

    def test_works_after_restore_merge_and_batch(self):
        t = self._build()
        expected = t.histogram_summary("x", [1, 5, 10])
        # 恢复（dict / JSON 文本）得到的样本同样可查询
        self.assertEqual(
            Telemetry.restore(t.snapshot()).histogram_summary("x", [1, 5, 10]),
            expected,
        )
        self.assertEqual(
            Telemetry.restore(t.json()).histogram_summary("x", [1, 5, 10]),
            expected,
        )
        # 合并进当前实例后同样可查询
        merged = Telemetry()
        merged.merge_snapshot(t.snapshot())
        self.assertEqual(
            merged.histogram_summary("x", [1, 5, 10]), expected
        )
        # 批量回放建立的序列同样可查询
        batched = Telemetry()
        batched.batch(
            [
                {"op": "observe", "name": "x", "value": value}
                for value in (0, 1, 2)
            ]
        )
        self.assertEqual(
            batched.histogram_summary("x", [1])["counts"], [2, 1]
        )

    def test_does_not_add_snapshot_json_or_digest_fields(self):
        t = self._build()
        t.histogram_summary("x", [1, 5, 10])
        snapshot = t.snapshot()
        self.assertEqual(set(snapshot), {"counters", "samples", "spans"})
        for record in snapshot["samples"]:
            self.assertNotIn("counts", record)
        encoded = t.json()
        self.assertNotIn("histogram_summary", encoded)
        # digest 不受查询影响
        self.assertEqual(t.digest(), Telemetry.restore(t.json()).digest())


class TelemetrySamplePercentileTest(unittest.TestCase):
    def _build(self):
        t = Telemetry()
        # 默认服务、无标签序列：1, 2
        t.observe("lat", 1)
        t.observe("lat", 2)
        # 默认服务、带标签序列：4, 8
        t.observe("lat", 4, labels=(("k", "v"),))
        t.observe("lat", 8, labels=(("k", "v"),))
        # api 服务、无标签序列：16, 32, 64
        t.observe("lat", 16, service="api")
        t.observe("lat", 32, service="api")
        t.observe("lat", 64, service="api")
        # 其他名称不应混入
        t.observe("other", 100)
        t.observe("other", 200, service="api")
        return t

    def test_merges_same_name_series_and_interpolates(self):
        t = self._build()
        # 合并后的数值副本升序为 [1, 2, 4, 8, 16, 32, 64]
        result = t.sample_percentile("lat", 50)
        self.assertEqual(result, 8.0)  # 位置 (7-1)*50/100 = 3，直接取第 3 项
        self.assertIsInstance(result, float)
        # 位置 1.5：在 2 与 4 之间线性插值
        self.assertEqual(t.sample_percentile("lat", 25), 3.0)
        # q 为 0 / 100 分别返回最小值 / 最大值
        self.assertEqual(t.sample_percentile("lat", 0), 1.0)
        self.assertEqual(t.sample_percentile("lat", 100), 64.0)
        # int 形式的 q 同样接受
        self.assertEqual(t.sample_percentile("lat", 100), 64.0)
        self.assertIsInstance(t.sample_percentile("lat", 0), float)

    def test_q_validation_rejects_bad_values_before_reading_samples(self):
        t = self._build()
        for bad in (True, False, "50", None, [50], object()):
            with self.assertRaises(ValueError):
                t.sample_percentile("lat", bad)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                t.sample_percentile("lat", bad)
        for bad in (-1, -0.5, 100.5, 101, 10 ** 400):
            with self.assertRaises(ValueError):
                t.sample_percentile("lat", bad)
        # 非法 q 先于历史值转换：坏 q + 坏数据并存时仍是参数 ValueError
        t.samples[("web", "lat", ())] = [object()]
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", float("nan"))
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", 200)

    def test_service_filter_none_empty_and_named(self):
        t = self._build()
        # 省略 service 与显式 None 等价：匹配全部服务
        self.assertEqual(t.sample_percentile("lat", 50), 8.0)
        self.assertEqual(
            t.sample_percentile("lat", 50),
            t.sample_percentile("lat", 50, service=None),
        )
        # 空字符串精确匹配默认服务：合并 [1, 2, 4, 8]，位置 1.5 插值为 3.0
        self.assertEqual(t.sample_percentile("lat", 50, service=""), 3.0)
        # 具名服务精确匹配：合并 [16, 32, 64]
        self.assertEqual(t.sample_percentile("lat", 50, service="api"), 32.0)
        self.assertIsNone(t.sample_percentile("lat", 50, service="web"))
        for bad in (1, 1.5, b"api", ["api"], object()):
            with self.assertRaises(ValueError):
                t.sample_percentile("lat", 50, service=bad)

    def test_labels_filter_none_empty_and_specific(self):
        t = self._build()
        # None 匹配全部标签集合
        self.assertEqual(t.sample_percentile("lat", 50, labels=None), 8.0)
        # 显式空序列只匹配无标签样本：合并 [1, 2, 16, 32, 64]，位置 2 取 16
        self.assertEqual(t.sample_percentile("lat", 50, labels=()), 16.0)
        # 具名标签按归一化后的完整集合精确匹配：合并 [4, 8]，位置 0.5 插值
        self.assertEqual(
            t.sample_percentile("lat", 50, labels=(("k", "v"),)), 6.0
        )
        # 标签输入顺序不影响匹配
        t.observe("lat2", 3, labels=(("a", 1), ("b", 2)))
        t.observe("lat2", 7, labels=(("a", 1), ("b", 2)))
        self.assertEqual(
            t.sample_percentile("lat2", 50, labels=(("b", 2), ("a", 1))), 5.0
        )
        # 没有该标签集合时返回 None
        self.assertIsNone(t.sample_percentile("lat", 50, labels=(("k", "x"),)))
        # 重复键、不可严格 JSON 序列化的值统一 ValueError
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", 50, labels=(("k", 1), ("k", 2)))
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", 50, labels=(("k", float("nan")),))

    def test_service_and_labels_filter_combined(self):
        t = self._build()
        self.assertEqual(
            t.sample_percentile("lat", 50, labels=(), service="api"), 32.0
        )
        self.assertIsNone(
            t.sample_percentile("lat", 50, labels=(("k", "v"),), service="api")
        )
        self.assertIsNone(
            t.sample_percentile("lat", 50, labels=(), service="web")
        )

    def test_missing_name_and_empty_series_return_none(self):
        t = self._build()
        self.assertIsNone(t.sample_percentile("nope", 50))
        # 空聚合器
        self.assertIsNone(Telemetry().sample_percentile("lat", 50))
        # 同名序列存在但全部为空（max_values_per_series=0）不参与合并
        trimmed = Telemetry(max_values_per_series=0)
        trimmed.observe("lat", 1)
        trimmed.observe("lat", 2, service="api")
        self.assertIsNone(trimmed.sample_percentile("lat", 50))
        # 一条非空加一条空：只统计非空序列
        mixed = Telemetry(max_values_per_series=1)
        mixed.observe("lat", 1)
        mixed.observe("lat", 2)  # 窗口只保留 [2]
        mixed.samples[("api", "lat", ())] = []  # 直接构造空序列
        self.assertEqual(mixed.sample_percentile("lat", 50), 2.0)

    def test_unhashable_name_rejected_before_reading_samples(self):
        t = self._build()
        with self.assertRaises(ValueError):
            t.sample_percentile(["lat"], 50)
        with self.assertRaises(ValueError):
            t.sample_percentile({"a": 1}, 50)

    def test_unconvertible_values_rejected_without_partial_result(self):
        t = Telemetry()
        t.observe("lat", 1)
        t.observe("lat", 2, service="api")
        # 不可转换的历史值：统一 ValueError，不返回部分结果
        t.samples[("web", "lat", ())] = [4, object()]
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", 50)
        # 超大整数溢出同样归一为 ValueError
        t.samples[("web", "lat", ())] = [10 ** 400]
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", 50)
        # 自定义 __float__ 抛异常也统一为 ValueError
        class ExplodingValue:
            def __float__(self):
                raise RuntimeError("boom")

        t.samples[("web", "lat", ())] = [ExplodingValue()]
        with self.assertRaises(ValueError):
            t.sample_percentile("lat", 50)
        # 筛选未命中坏数据序列时不受影响
        self.assertEqual(t.sample_percentile("lat", 50, service=""), 1.0)

    def test_does_not_read_clock_or_mutate_state(self):
        class BoomClock:
            def __call__(self):
                raise AssertionError("clock must not be called")

        t = Telemetry(BoomClock())
        t.observe("lat", 3)
        t.observe("lat", 1)
        t.observe("lat", 2, service="api")
        before = t.snapshot()
        before_digest = t.digest()
        before_values = {
            key: list(values) for key, values in t.samples.items()
        }
        self.assertEqual(t.sample_percentile("lat", 50), 2.0)
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(t.digest(), before_digest)
        # values 顺序不被排序副本回写
        self.assertEqual(t.samples, before_values)
        # 输入标签不被修改
        labels = [("b", 2), ("a", 1)]
        t.observe("tagged", 5, labels=labels)
        t.sample_percentile("tagged", 50, labels=labels)
        self.assertEqual(labels, [("b", 2), ("a", 1)])

    def test_result_is_repeatable_plain_float(self):
        t = self._build()
        first = t.sample_percentile("lat", 25)
        second = t.sample_percentile("lat", 25)
        self.assertEqual(first, second)
        self.assertIsInstance(first, float)
        self.assertNotIsInstance(first, bool)

    def test_works_after_restore_merge_and_batch(self):
        t = self._build()
        expected = t.sample_percentile("lat", 50)
        # restore 得到的样本遵循同一规则
        restored = Telemetry.restore(t.json())
        self.assertEqual(restored.sample_percentile("lat", 50), expected)
        # merge_snapshot 得到的样本同样可查询
        merged = Telemetry()
        merged.merge_snapshot(t.snapshot())
        self.assertEqual(merged.sample_percentile("lat", 50), expected)
        # batch 写入的样本同样可查询
        batched = Telemetry()
        batched.batch(
            [
                {"op": "observe", "name": "x", "value": value}
                for value in (0, 1, 2, 3)
            ]
        )
        self.assertEqual(batched.sample_percentile("x", 50), 1.5)

    def test_does_not_add_snapshot_json_or_digest_fields(self):
        t = self._build()
        t.sample_percentile("lat", 50)
        snapshot = t.snapshot()
        self.assertEqual(set(snapshot), {"counters", "samples", "spans"})
        encoded = t.json()
        self.assertNotIn("sample_percentile", encoded)
        # digest 不受查询影响
        self.assertEqual(t.digest(), Telemetry.restore(t.json()).digest())


class TelemetrySpansByStartTimeTest(unittest.TestCase):
    def build(self):
        t = Telemetry(iter(range(1000)).__next__)
        t.start("a")                          # start 0
        t.finish("a", error="boom")          # end 1, error
        t.start("b", service="api")          # start 2
        t.finish("b", service="api")         # end 3, closed 无异常
        t.start("c", service="api")          # start 4, 仍 open
        t.start("d", service="api", labels=(("k", "v"),))  # start 5
        t.finish("d", service="api", error=0)             # end 6, error
        return t

    def test_default_keeps_open_spans_and_shape_is_query(self):
        t = self.build()
        # status 缺省保留全部跨度（含未结束的 c），排序沿用服务、start、span。
        rows = t.spans_by_start_time()
        self.assertEqual([e["span"] for e in rows], ["a", "b", "c", "d"])
        for row in rows:
            # 与 query 同形：不附加 duration 或其他派生字段。
            self.assertIn(
                set(row),
                (
                    {"span", "service", "parent", "start", "end", "error"},
                    {"span", "service", "parent", "start", "end", "error",
                     "labels"},
                ),
            )
            self.assertNotIn("duration", row)
        # 无标签跨度不带 labels；有标签跨度保留可修改的 labels。
        self.assertNotIn("labels", rows[0])
        self.assertEqual(rows[3]["labels"], [("k", "v")])
        # 与无筛选 snapshot 的跨度数组逐项一致（同形、同序、同值）。
        self.assertEqual(rows, t.snapshot()["spans"])

    def test_closed_interval_boundaries_inclusive(self):
        t = Telemetry(iter(range(100)).__next__)
        for name in ("p1", "p3", "p10"):
            t.start(name)
            t.finish(name)
        # p1 start 0、p3 start 2、p10 start 4
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(minimum=2, maximum=4)],
            ["p3", "p10"],
        )
        # 边界闭区间：恰好等于边界的 start 被包含。
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(minimum=2, maximum=2)],
            ["p3"],
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(maximum=2)],
            ["p1", "p3"],
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(minimum=2)],
            ["p3", "p10"],
        )
        # 省略两侧即无界。
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time()],
            ["p1", "p3", "p10"],
        )
        # 区间内无匹配返回空列表。
        self.assertEqual(
            t.spans_by_start_time(minimum=2.5, maximum=3.5), []
        )

    def test_open_spans_only_have_start_validated(self):
        t = self.build()
        # status=open 只命中未结束的 c。
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(status="open")], ["c"]
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(status="closed")],
            ["a", "b", "d"],
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(status="error")],
            ["a", "d"],
        )
        # 未结束跨度的 start 仍需转换，但其为空的 end 不被接触：
        # 非法 start 抛错，非法 end 不影响查询。
        x = Telemetry()
        x.spans[("", "badstart")] = {
            "parent": None, "start": "nope", "end": None,
            "error": None, "labels": (),
        }
        with self.assertRaises(ValueError):
            x.spans_by_start_time()
        with self.assertRaises(ValueError):
            x.spans_by_start_time(status="open")
        y = Telemetry()
        y.spans[("", "badend")] = {
            "parent": None, "start": 1, "end": "nope",
            "error": None, "labels": (),
        }
        self.assertEqual(
            [e["span"] for e in y.spans_by_start_time()], ["badend"]
        )

    def test_status_uses_query_definitions(self):
        t = self.build()
        for good in (None, "open", "closed", "error"):
            t.spans_by_start_time(status=good)
        for bad in ("done", "", "OPEN", 0, True, b"open", ["x"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.spans_by_start_time(status=bad)

    def test_bounds_must_be_non_bool_finite_int_or_float(self):
        t = self.build()
        for bad in (
            True, False, "1", b"1", 1j, object(),
            float("nan"), float("inf"), float("-inf"),
        ):
            with self.assertRaises(ValueError, msg=("minimum", repr(bad))):
                t.spans_by_start_time(minimum=bad)
            with self.assertRaises(ValueError, msg=("maximum", repr(bad))):
                t.spans_by_start_time(maximum=bad)
        # int（含 0、负数、超大整数）与有限 float 合法。
        t.spans_by_start_time(minimum=0, maximum=10 ** 100)
        t.spans_by_start_time(minimum=-5.0, maximum=0.0)
        t.spans_by_start_time(minimum=1, maximum=1)

    def test_minimum_greater_than_maximum_raises(self):
        t = self.build()
        for lo, hi in ((2, 1), (1.1, 1.0), (10 ** 100, 1)):
            with self.assertRaises(ValueError, msg=(lo, hi)):
                t.spans_by_start_time(minimum=lo, maximum=hi)
        # 相等合法（闭区间）。
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(minimum=2, maximum=2)],
            ["b"],
        )

    def test_service_and_labels_filters_match_query_semantics(self):
        t = self.build()
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(service="api")],
            ["b", "c", "d"],
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(service="")], ["a"]
        )
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(labels=())],
            ["a", "b", "c"],
        )
        self.assertEqual(
            [e["span"]
             for e in t.spans_by_start_time(labels=(("k", "v"),))],
            ["d"],
        )
        self.assertEqual(t.spans_by_start_time(labels=(("k", "missing"),)), [])
        self.assertEqual(
            [e["span"] for e in t.spans_by_start_time(
                service="api", status="open")],
            ["c"],
        )
        # 区间与筛选叠加。
        self.assertEqual(
            [e["span"]
             for e in t.spans_by_start_time(service="api", minimum=3)],
            ["c", "d"],
        )

    def test_invalid_service_and_labels_raise_valueerror(self):
        t = self.build()
        for bad in (1, b"api", ["api"], True, object()):
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.spans_by_start_time(service=bad)
        with self.assertRaises(ValueError):
            t.spans_by_start_time(labels=(("k", 1), ("k", 2)))  # 重复键
        with self.assertRaises(ValueError):
            t.spans_by_start_time(labels=(("v", object()),))    # 不可序列化
        with self.assertRaises(ValueError):
            t.spans_by_start_time(labels=(("v", float("nan")),))
        with self.assertRaises(ValueError):
            t.spans_by_start_time(labels="bad")
        # 全部条件先校验：区间矛盾 + 非法标签同时存在仍是 ValueError，
        # 且即使服务本身无匹配，非法标签也在读取数据前拒绝。
        with self.assertRaises(ValueError):
            t.spans_by_start_time(minimum=5, maximum=1, labels="bad")
        with self.assertRaises(ValueError):
            t.spans_by_start_time(service="nope", labels="bad")

    def test_validation_before_reading_spans_no_partial_result(self):
        class AssertingClock:
            reads = 0

            def __call__(self):
                self.reads += 1
                raise AssertionError("clock must not be read")

        t = self.build()
        before = t.snapshot()
        t.clock = AssertingClock()
        t.spans_by_start_time()
        t.spans_by_start_time(status="open")
        t.spans_by_start_time(status="error")
        t.spans_by_start_time(minimum=1.0, maximum=4.0, service="api",
                              labels=())
        for bad in ("done", 0, True):
            with self.assertRaises(ValueError):
                t.spans_by_start_time(status=bad)
        with self.assertRaises(ValueError):
            t.spans_by_start_time(service=1)
        with self.assertRaises(ValueError):
            t.spans_by_start_time(minimum=float("inf"))
        with self.assertRaises(ValueError):
            t.spans_by_start_time(minimum=9, maximum=1)
        self.assertEqual(AssertingClock.reads, 0)
        t.clock = iter(range(100)).__next__
        self.assertEqual(t.snapshot(), before)

    def test_non_finite_starts_raise_without_partial_result(self):
        def open_span_with(start):
            x = Telemetry()
            x.spans[("", "s")] = {
                "parent": None, "start": start, "end": None,
                "error": None, "labels": (),
            }
            return x

        for bad in ("not-a-time", float("nan"), float("inf"),
                    float("-inf"), object(), 10 ** 400):
            x = open_span_with(bad)
            before = x.snapshot()
            with self.assertRaises(ValueError, msg=repr(bad)):
                x.spans_by_start_time()
            # 未结束跨度只校验 start：open 筛选同样拒绝。
            with self.assertRaises(ValueError, msg=repr(bad)):
                x.spans_by_start_time(status="open")
            self.assertEqual(x.snapshot(), before)

        # 一条正常 + 一条异常：全部候选先转换，整体 ValueError 无部分结果；
        # 即使异常跨度落在请求区间之外也一样先转换（两条都 open）。
        x = Telemetry()
        x.spans[("", "bad")] = {
            "parent": None, "start": "oops", "end": None,
            "error": None, "labels": (),
        }
        x.spans[("", "good")] = {
            "parent": None, "start": 100.0, "end": None,
            "error": None, "labels": (),
        }
        with self.assertRaises(ValueError):
            x.spans_by_start_time(minimum=50.0, maximum=60.0)

        # 数字字符串沿用样本浮点规则正常转换；记录保留原值。
        x = open_span_with("4.5")
        row = x.spans_by_start_time(minimum=4)[0]
        self.assertEqual(row["start"], "4.5")

    def test_result_is_fresh_independent_and_repeatable(self):
        t = self.build()
        first = t.spans_by_start_time()
        second = t.spans_by_start_time()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[3]["labels"], second[3]["labels"])
        first[1]["start"] = 999
        first[1]["error"] = "changed"
        first[3]["labels"].append(("z", 1))
        first.append("junk")
        again = t.spans_by_start_time()
        self.assertEqual(len(again), 4)
        self.assertEqual([e["span"] for e in again], ["a", "b", "c", "d"])
        self.assertEqual(again[1]["start"], 2)
        self.assertIsNone(again[1]["error"])
        self.assertEqual(again[3]["labels"], [("k", "v")])

    def test_exception_instance_error_preserved(self):
        t = Telemetry(iter(range(2)).__next__)
        t.start("e")
        t.finish("e", error=ValueError("x"))
        row = t.spans_by_start_time(status="error")[0]
        self.assertIsInstance(row["error"], ValueError)
        self.assertEqual(
            json.loads(t.json(status="error"))["spans"][0]["error"],
            {"type": "ValueError", "message": "x"},
        )

    def test_empty_aggregator_returns_empty_list(self):
        self.assertEqual(Telemetry().spans_by_start_time(), [])
        self.assertEqual(
            Telemetry().spans_by_start_time(minimum=0, maximum=10), []
        )

    def test_order_follows_service_start_span(self):
        t = Telemetry(iter([10, 12,    # api q：start 10
                            0, 1,      # 默认服务 p：start 0
                            2, 5,      # api a：start 2
                            4]).__next__)  # api m：start 4，open
        t.start("q", service="api"); t.finish("q", service="api")
        t.start("p"); t.finish("p")
        t.start("a", service="api"); t.finish("a", service="api")
        t.start("m", service="api")
        self.assertEqual(
            [(e["service"], e["span"], e["start"])
             for e in t.spans_by_start_time()],
            [("", "p", 0), ("api", "a", 2), ("api", "m", 4),
             ("api", "q", 10)],
        )

    def test_not_written_to_snapshot_json_or_digest(self):
        t = self.build()
        before = t.snapshot()
        t.spans_by_start_time()
        t.spans_by_start_time(status="error")
        t.spans_by_start_time(minimum=0.0, maximum=100.0, service="api",
                              labels=(("k", "v"),))
        self.assertEqual(t.snapshot(), before)
        self.assertEqual(set(t.snapshot()), {"counters", "samples", "spans"})
        for record in t.snapshot()["spans"]:
            self.assertNotIn("start_time", record)
        payload = json.loads(t.json())
        self.assertTrue(all("start_time" not in r for r in payload["spans"]))
        self.assertEqual(t.digest(), Telemetry.restore(t.json()).digest())
        # 既有 spans_by_duration 结果不受影响。
        self.assertEqual(
            [e["span"] for e in t.spans_by_duration()], ["a", "b", "d"]
        )

    def test_after_restore_merge_and_batch(self):
        base = self.build()
        expected = [(e["span"], e["start"]) for e in base.spans_by_start_time()]
        restored = Telemetry.restore(base.json()).spans_by_start_time()
        self.assertEqual(
            [(e["span"], e["start"]) for e in restored], expected
        )
        from_snap = Telemetry.from_snapshot(
            base.snapshot()
        ).spans_by_start_time()
        self.assertEqual(
            [(e["span"], e["start"]) for e in from_snap], expected
        )
        merged = Telemetry()
        self.assertIsNone(merged.merge_snapshot(base.snapshot()))
        self.assertEqual(
            [(e["span"], e["start"]) for e in merged.spans_by_start_time()],
            expected,
        )
        # 多片合并与批量回放得到的跨度同样可按开始时间查询。
        shard = Telemetry(iter(range(2)).__next__)
        shard.start("z", service="shard")
        shard.finish("z", service="shard")
        merged.merge_snapshots([shard.snapshot()])
        self.assertEqual(
            [e["span"]
             for e in merged.spans_by_start_time(service="shard")],
            ["z"],
        )
        batched = Telemetry(iter(range(2)).__next__)
        batched.batch([
            {"op": "start", "span": "w", "service": "b"},
            {"op": "finish", "span": "w", "service": "b"},
        ])
        self.assertEqual(
            [e["span"]
             for e in batched.spans_by_start_time(service="b")],
            ["w"],
        )


class TelemetrySpanContextTest(unittest.TestCase):
    def test_empty_block_closes_span_with_two_clock_reads(self):
        ticks = iter(range(10, 100, 10))
        calls = []
        def clock():
            value = next(ticks)
            calls.append(value)
            return value
        t = Telemetry(clock)
        with t.span("r") as scope:
            # 进入后只读了一次 clock，记录处于 open 状态
            self.assertEqual(calls, [10])
            self.assertEqual(t.query("open")[0]["span"], "r")
            self.assertIsNone(scope.end)
        self.assertEqual(calls, [10, 20])  # 进出各读一次
        entry = t.query("closed")[0]
        self.assertEqual(
            set(entry), {"span", "service", "parent", "start", "end", "error"}
        )
        self.assertEqual((entry["start"], entry["end"]), (10, 20))
        self.assertIsNone(entry["error"])
        self.assertEqual(t.query("open"), [])

    def test_scope_attributes_and_isolation(self):
        t = Telemetry(iter(range(100)).__next__)
        parent = ["p"]
        with t.span("s", parent=parent, service="api",
                    labels=(("k", "v"),)) as scope:
            self.assertEqual(scope.span, "s")
            self.assertEqual(scope.service, "api")
            self.assertEqual(scope.parent, ["p"])
            self.assertEqual(scope.start, 0)
            self.assertIsNone(scope.end)
            self.assertIsNone(scope.error)
            # 修改读取结果不回写聚合器
            scope.parent.append("x")
            self.assertEqual(scope.parent, ["p"])
            self.assertEqual(t.query("open")[0]["parent"], ["p"])
        self.assertEqual(scope.end, 1)
        self.assertIsNone(scope.error)
        # 退出后属性仍只读反映最终状态，且与聚合器隔离
        self.assertEqual(t.trace("s", service="api")["parent"], ["p"])

    def test_exception_records_error_and_propagates(self):
        t = Telemetry(iter(range(100)).__next__)
        boom = RuntimeError("boom")
        with self.assertRaises(RuntimeError) as caught:
            with t.span("r", service="api") as scope:
                raise boom
        self.assertIs(caught.exception, boom)  # 原异常继续传播
        entry = t.query("error")[0]
        self.assertIs(entry["error"], boom)  # 同一个异常对象
        self.assertEqual(entry["end"], 1)
        self.assertEqual(t.query("open"), [])
        # json 对不可严格表示的异常沿用既有占位规则
        payload = json.loads(t.json())
        self.assertEqual(
            payload["spans"][0]["error"],
            {"type": "RuntimeError", "message": "boom"},
        )
        self.assertEqual(scope.error, boom)

    def test_enter_validation_failures_leave_no_record_no_clock(self):
        def clock():
            raise AssertionError("clock must not be read")
        t = Telemetry(clock)
        for make in (
            lambda: t.span("x", service=""),
            lambda: t.span("x", service=1),
            lambda: t.span(["unhashable"]),
            lambda: t.span("x", labels=(("k", 1), ("k", 2))),
            lambda: t.span("x", labels=(("k", float("nan")),)),
        ):
            with self.assertRaises(ValueError):
                with make():
                    pass
        self.assertEqual(t.snapshot()["spans"], [])
        # 重复标识沿用 ValueError，同样不留记录、不读 clock
        t3 = Telemetry()
        t3.start("dup")
        t3.clock = clock
        with self.assertRaises(ValueError):
            with t3.span("dup"):
                pass

    def test_capacity_error_on_enter(self):
        t = Telemetry(max_spans=1)
        with t.span("a"):
            pass
        before = t.snapshot()
        with self.assertRaises(TelemetryCapacityError):
            with t.span("b"):
                pass
        self.assertEqual(t.snapshot(), before)  # 拒绝不改变聚合
        # 容量不超限时正常记录
        t2 = Telemetry(iter(range(10)).__next__, max_spans=1)
        with t2.span("only"):
            pass
        self.assertEqual(len(t2.query("closed")), 1)

    def test_reuse_and_refinish_rejected_without_second_timestamp(self):
        ticks = iter(range(100))
        t = Telemetry(lambda: next(ticks))
        scope = t.span("r")
        with scope:
            pass
        with self.assertRaises(ValueError):
            with scope:  # 重复使用
                pass
        with self.assertRaises(ValueError):
            scope.__exit__(None, None, None)  # 再次退出
        with self.assertRaises(ValueError):
            t.finish("r")  # 上下文退出后再次结束
        self.assertEqual(t.query("closed")[0]["end"], 1)
        self.assertEqual(len(t.snapshot()["spans"]), 1)

    def test_clock_failure_on_finish_keeps_pre_finish_state(self):
        calls = []

        def clock():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("clock broke")
            return 0

        t = Telemetry(clock)
        with self.assertRaises(RuntimeError):
            with t.span("r"):
                pass
        entry = t.query("open")[0]  # end 未写入，仍是 open
        self.assertEqual(entry["start"], 0)
        self.assertIsNone(entry["end"])
        self.assertIsNone(entry["error"])

        # 异常路径：error 已先写入，clock 失败时 end 仍为空、error 保留
        calls.clear()
        t2 = Telemetry(clock)
        boom = KeyError("k")
        with self.assertRaises(RuntimeError):
            with t2.span("r"):
                raise boom
        entry = t2.query("open")[0]
        self.assertIsNone(entry["end"])
        self.assertIs(entry["error"], boom)

    def test_nesting_cross_service_and_explicit_parent(self):
        t = Telemetry(iter(range(100)).__next__)
        with t.span("outer", service="a"):
            with t.span("inner", parent="outer", service="a"):
                pass
            with t.span("outer", service="b"):  # 跨 service 同名隔离
                pass
        a_closed = t.query("closed", service="a")
        # 快照稳定顺序按服务、开始时间、标识：outer 先于 inner 开始
        self.assertEqual([e["span"] for e in a_closed], ["outer", "inner"])
        tree = t.trace("outer", service="a")
        self.assertEqual([c["span"] for c in tree["children"]], ["inner"])
        # parent 只采用显式传入值：未传 parent 的嵌套跨度不自动挂父子
        self.assertIsNone(t.trace("outer", service="b")["parent"])
        self.assertEqual(
            [e["span"] for e in t.query("closed", service="b")], ["outer"]
        )

    def test_equivalence_with_start_finish_and_offline_entries(self):
        # 父子引用须同服务可解析（restore 的严格检查），两个跨度同放 api
        manual = Telemetry(iter(range(100)).__next__)
        manual.start("s1", service="api")
        manual.start("s2", parent="s1", service="api")
        manual.finish("s2", service="api")
        manual.finish("s1", service="api")

        scoped = Telemetry(iter(range(100)).__next__)
        with scoped.span("s1", service="api"):
            with scoped.span("s2", parent="s1", service="api"):
                pass
        self.assertEqual(scoped.snapshot(), manual.snapshot())
        self.assertEqual(scoped.json(), manual.json())
        self.assertEqual(scoped.digest(), manual.digest())

        # batch 后续事件可继续作用于 with 创建的跨度
        t = Telemetry(iter(range(100)).__next__)
        with t.span("b"):
            pass
        t.batch([{"op": "start", "span": "c"}, {"op": "finish", "span": "c"}])
        self.assertEqual(len(t.query("closed")), 2)

        # restore / merge 结果与手动记录完全一致
        restored = Telemetry.restore(scoped.json())
        self.assertEqual(restored.snapshot(), manual.snapshot())
        merged = Telemetry()
        merged.merge_snapshot(scoped.snapshot())
        self.assertEqual(merged.snapshot(), manual.snapshot())
        diff = Telemetry.diff_snapshots(manual.snapshot(), scoped.snapshot())
        for section in ("counters", "samples", "spans"):
            self.assertEqual(diff[section]["added"], [])
            self.assertEqual(diff[section]["removed"], [])
            self.assertEqual(diff[section]["changed"], [])

    def test_duration_queries_and_snapshot_shape_unchanged(self):
        t = Telemetry(iter(range(0, 100, 5)).__next__)
        with t.span("d", labels=(("k", "v"),)):
            pass
        stats = t.span_duration_stats()
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["values"], [5.0])
        entry = t.snapshot()["spans"][0]
        # 快照字段形状与 start/finish 记录一致（有标签时附加 labels）
        self.assertEqual(
            set(entry),
            {"span", "service", "parent", "start", "end", "error", "labels"},
        )
        self.assertEqual(entry["labels"], [("k", "v")])


if __name__ == "__main__":
    unittest.main()
