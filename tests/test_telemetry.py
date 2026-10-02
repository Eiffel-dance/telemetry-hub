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
        self.assertEqual([(e["service"], e["span"]) for e in opened], [("api", "r1")])
        self.assertEqual([(e["service"], e["span"]) for e in errored], [("", "r1")])
        self.assertEqual(
            set(errored[0]),
            {"span", "service", "parent", "start", "end", "error"},
        )
        self.assertIsNone(errored[0]["parent"])
        self.assertEqual(errored[0]["error"], "boom")
        self.assertIsNotNone(errored[0]["end"])
        with self.assertRaises(ValueError):
            t.query("closed")
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
    def build_pair(self):
        a = Telemetry(iter(range(100)).__next__)
        a.inc("hits", 2)
        a.inc("only_a", 5)
        a.observe("lat", 1, service="api")
        a.observe("lat", 2.5, service="api")
        a.start("s1")
        a.finish("s1", error="boom")
        a.start("open_a")

        b = Telemetry(iter(range(200, 400)).__next__)
        b.inc("hits", 4)                            # 与 a 同键 -> 累加
        b.inc("hits", 3, service="api")             # 新记录
        b.observe("lat", "3.5", service="api")      # 追加到同一样本
        b.observe("lat", 9, service="web")
        b.spans[("", "s1")] = {                     # 与 a 完全一致 -> 幂等
            "parent": None, "start": 0, "end": 1, "error": "boom",
        }
        b.start("open_b", service="api", parent="p")
        return a, b

    def test_merge_basic_accumulation(self):
        a, b = self.build_pair()
        self.assertIsNone(a.merge_snapshot(b.snapshot()))
        snap = a.snapshot()
        counters = {(c["service"], c["name"]): c["value"]
                    for c in snap["counters"]}
        self.assertEqual(counters, {
            ("", "hits"): 6, ("", "only_a"): 5, ("api", "hits"): 3,
        })
        lat = {s["service"]: s for s in snap["samples"] if s["name"] == "lat"}
        self.assertEqual(lat["api"]["values"], [1, 2.5, "3.5"])
        self.assertEqual(lat["api"]["count"], 3)
        self.assertEqual(lat["api"]["sum"], 7.0)
        self.assertEqual(lat["api"]["minimum"], 1.0)
        self.assertEqual(lat["api"]["maximum"], 3.5)
        self.assertEqual(lat["api"]["mean"], 7.0 / 3)
        self.assertEqual(lat["web"]["values"], [9])

    def test_merge_accepts_text_and_bytes(self):
        _, b = self.build_pair()
        for payload in (b.json(), b.json().encode("utf-8")):
            t = Telemetry()
            self.assertIsNone(t.merge_snapshot(payload))
            self.assertEqual(t.snapshot(), b.snapshot())

    def test_merge_label_normalization_keys(self):
        a = Telemetry()
        a.inc("k", 1, labels=(("a", 1), ("b", 2)))
        b = Telemetry()
        b.inc("k", 1, labels=(("b", 2), ("a", 1)))  # 归一化后同键
        a.merge_snapshot(b.snapshot())
        counters = a.snapshot()["counters"]
        self.assertEqual(len(counters), 1)
        self.assertEqual(counters[0]["labels"], [("a", 1), ("b", 2)])
        self.assertEqual(counters[0]["value"], 2)

    def test_merge_spans_query_and_finish(self):
        a, b = self.build_pair()
        a.merge_snapshot(b.snapshot())
        self.assertEqual(
            [(e["service"], e["span"]) for e in a.query("open")],
            [("", "open_a"), ("api", "open_b")],
        )
        self.assertEqual(
            [(e["service"], e["span"]) for e in a.query("error")],
            [("", "s1")],
        )
        # 来自输入的未结束跨度可按服务结束，父标识原值保留
        a.finish("open_b", service="api", error="late")
        self.assertEqual(
            [e["span"] for e in a.query("open")], ["open_a"]
        )
        record = a.spans[("api", "open_b")]
        self.assertEqual(record["parent"], "p")
        self.assertEqual(record["error"], "late")

    def test_merge_does_not_mutate_or_share_input(self):
        import copy
        payload = {
            "counters": [{"service": "", "name": "c",
                          "labels": [["k", "v"]], "value": 1}],
            "samples": [{"service": "", "name": "s",
                         "labels": [["k", "v"]], "values": [1]}],
            "spans": [{"span": "z", "service": "", "parent": {"trace": 1},
                       "start": 0, "end": None, "error": None}],
        }
        original = copy.deepcopy(payload)
        a = Telemetry()
        a.merge_snapshot(payload)
        self.assertEqual(payload, original)  # 输入未被改写
        # 改写 payload 的所有嵌套可变对象不影响合并结果
        payload["counters"][0]["value"] = 999
        payload["counters"][0]["labels"][0][1] = "changed"
        payload["samples"][0]["values"].append(1234)
        payload["spans"][0]["parent"]["trace"] = 99
        merged = a.snapshot()
        self.assertEqual(merged["counters"][0]["value"], 1)
        self.assertEqual(merged["counters"][0]["labels"], [("k", "v")])
        self.assertEqual(merged["samples"][0]["values"], [1])
        self.assertEqual(merged["spans"][0]["parent"], {"trace": 1})

    def test_merge_invalid_payload_rolls_back(self):
        a, _ = self.build_pair()
        import copy
        before = copy.deepcopy(a.snapshot())
        bad_payloads = [
            "{not json}",
            b"\xff",
            {"counters": [], "samples": []},                       # 缺 spans
            {"counters": [], "samples": [], "spans": [], "x": 1},  # 多余字段
            {"counters": "no", "samples": [], "spans": []},
            {"counters": [{"service": "", "name": "x",
                           "labels": [["k", 1], ["k", 2]], "value": 1}],
             "samples": [], "spans": []},                          # 无效标签
            {"counters": [], "samples": [{"service": "", "name": "m",
                                          "labels": [], "values": [float("nan")]}],
             "spans": []},                                         # 非有限样本
            {"counters": [], "samples": [], "spans": [
                {"span": ["unhashable"], "service": "", "parent": None,
                 "start": 0, "end": None, "error": None}]},        # 不可哈希标识
            '{"counters":[],"samples":[],"spans":[],"x":NaN}',     # 非严格 JSON
        ]
        for bad in bad_payloads:
            with self.assertRaises(ValueError, msg=repr(bad)):
                a.merge_snapshot(bad)
            self.assertEqual(a.snapshot(), before)
        # 时钟配置不变
        self.assertEqual(a.clock.__name__ if hasattr(a.clock, "__name__") else None,
                         "__next__")

    def test_merge_counter_add_failure_rolls_back(self):
        a, b = Telemetry(), Telemetry()
        a.counters[("", "x", ())] = "str"
        b.inc("x", 1)
        import copy
        before = copy.deepcopy(a.snapshot())
        with self.assertRaises(ValueError):
            a.merge_snapshot(b.snapshot())
        self.assertEqual(a.snapshot(), before)

    def test_merge_conflicting_span_rolls_back_all(self):
        a = Telemetry(iter(range(10)).__next__)
        a.inc("h", 1)
        a.observe("m", 1)
        a.start("s")
        a.finish("s")
        b = Telemetry(iter(range(10)).__next__)
        b.inc("h", 2)          # 本可累加
        b.observe("m", 2)      # 本可追加
        b.start("s")           # 同为 open/closed 冲突（a 已结束，b 未结束）
        import copy
        before = copy.deepcopy(a.snapshot())
        with self.assertRaises(ValueError):
            a.merge_snapshot(b.snapshot())
        self.assertEqual(a.snapshot(), before)  # 全部撤销
        self.assertEqual(a.query("error"), [])

    def test_merge_identical_span_is_idempotent(self):
        a, b = self.build_pair()
        a.merge_snapshot(b.snapshot())
        spans = a.snapshot()["spans"]
        self.assertEqual(
            [e["span"] for e in spans if e["span"] == "s1"].count("s1"), 1
        )
        # 再次合并同样的跨度仍成功
        a.merge_snapshot(b.snapshot())
        self.assertEqual(
            len([e for e in a.snapshot()["spans"] if e["span"] == "s1"]), 1
        )

    def test_merge_empty_sample_keeps_no_stats(self):
        t = Telemetry()
        t.samples[("", "e", ())] = []
        t.merge_snapshot({"counters": [], "samples": [], "spans": []})
        entry = t.snapshot()["samples"][0]
        self.assertEqual(set(entry), {"service", "name", "labels", "values"})

    def test_merge_stable_json_and_no_shared_span_objects(self):
        a, b = self.build_pair()
        a.merge_snapshot(b.snapshot())
        self.assertEqual(
            a.json(),
            json.dumps(a.snapshot(), sort_keys=True, separators=(",", ":")),
        )
        # 入站跨度的嵌套对象不共享
        payload = {"counters": [], "samples": [], "spans": [
            {"span": "w", "service": "", "parent": {"trace": 1},
             "start": 1, "end": None, "error": None}]}
        t = Telemetry()
        t.merge_snapshot(payload)
        payload["spans"][0]["parent"]["trace"] = 99
        self.assertEqual(t.spans[("", "w")]["parent"], {"trace": 1})

    def test_merge_preserves_non_strict_error_placeholder(self):
        src = Telemetry(iter(range(3)).__next__)
        src.start("q")
        src.finish("q", error=ValueError("x"))
        t = Telemetry()
        with self.assertRaises(ValueError):  # dict 形式携带不可表示异常
            t.merge_snapshot(src.snapshot())
        self.assertEqual(t.spans, {})
        t.merge_snapshot(src.json())        # JSON 形式已是占位对象
        self.assertEqual(
            t.query("error")[0]["error"],
            {"type": "ValueError", "message": "x"},
        )


if __name__ == "__main__":
    unittest.main()
