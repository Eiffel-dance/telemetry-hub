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


class TelemetryFromSnapshotTest(unittest.TestCase):
    def make_telemetry(self):
        t = Telemetry(iter(range(100, 200)).__next__)
        t.inc("hits", 2, labels=(("b", "2"), ("a", "1")))
        t.inc("hits", service="api")
        t.observe("lat", 1, service="api")
        t.observe("lat", 2.5, service="api")
        t.observe("lat", "3.5", service="api")
        t.start("open-span", parent="p", service="api")
        t.start("done-span")
        t.finish("done-span", error="boom")
        return t

    def test_roundtrip_via_snapshot_object(self):
        t = self.make_telemetry()
        snap = t.snapshot()
        restored = Telemetry.from_snapshot(snap)
        self.assertEqual(restored.snapshot(), snap)
        self.assertEqual(restored.json(), t.json())
        self.assertEqual(restored.query("open"), t.query("open"))
        self.assertEqual(restored.query("error"), t.query("error"))
        # open 跨度可在新实例上结束
        restored.finish("open-span", service="api")
        self.assertEqual(restored.query("open"), [])
        self.assertEqual(len(t.query("open")), 1)  # 原实例不受影响

    def test_roundtrip_via_json_text(self):
        t = self.make_telemetry()
        restored = Telemetry.from_snapshot(t.json())
        self.assertEqual(restored.json(), t.json())
        self.assertEqual(restored.snapshot(), t.snapshot())

    def test_no_sharing_with_input(self):
        t = self.make_telemetry()
        snap = t.snapshot()
        restored = Telemetry.from_snapshot(snap)
        snap["samples"][0]["values"].append(999)
        snap["spans"][0]["error"] = "mutated"
        snap["counters"].append({"service": "", "name": "x", "labels": [], "value": 1})
        self.assertEqual(restored.snapshot(), t.snapshot())
        # 反向：新实例继续写入不影响原实例
        restored.inc("hits")
        restored.observe("lat", 99, service="api")
        self.assertNotEqual(restored.snapshot(), t.snapshot())
        self.assertEqual(
            [c["value"] for c in t.snapshot()["counters"] if c["service"] == ""],
            [2],
        )

    def test_non_strict_error_requires_json_form(self):
        t = Telemetry(iter(range(100)).__next__)
        t.start("s")
        t.finish("s", error=ValueError("x"))
        with self.assertRaises(ValueError):
            Telemetry.from_snapshot(t.snapshot())
        restored = Telemetry.from_snapshot(t.json())
        self.assertEqual(
            restored.query("error")[0]["error"],
            {"type": "ValueError", "message": "x"},
        )

    def test_empty_values_sample_restored_without_stats(self):
        payload = {
            "counters": [],
            "samples": [
                {"service": "", "name": "e", "labels": [], "values": []}
            ],
            "spans": [],
        }
        restored = Telemetry.from_snapshot(payload)
        entry = restored.snapshot()["samples"][0]
        self.assertEqual(set(entry), {"service", "name", "labels", "values"})

    def test_invalid_payloads(self):
        t = self.make_telemetry()
        good = t.snapshot()
        bad_payloads = [
            "not json",
            42,
            [],
            {"counters": [], "samples": []},                              # 缺顶层字段
            {"counters": [], "samples": [], "spans": [], "extra": []},    # 多顶层字段
            {"counters": {}, "samples": [], "spans": []},                 # 非数组
            '{"counters":[],"samples":[],"spans":[],"counters":[]}',      # 重复顶层键
            '{"counters":[{"service":"","name":"a","labels":[],"value":NaN}],'
            '"samples":[],"spans":[]}',                                   # 非法常量
        ]
        bad_payloads.append({"counters": [{"service": "", "name": "a", "labels": []}],  # 缺 value
                             "samples": [], "spans": []})
        bad_payloads.append({"counters": [{"service": "", "name": "a", "labels": [],
                                           "value": 1, "x": 1}],          # 多字段
                             "samples": [], "spans": []})
        dup = dict(good)
        dup["counters"] = good["counters"][:1] + good["counters"][:1]     # 重复记录
        bad_payloads.append(dup)
        mismatch = json.loads(t.json())
        mismatch["samples"][0]["count"] = 99                              # 统计不一致
        bad_payloads.append(mismatch)
        partial = json.loads(t.json())
        partial["samples"][0] = {"service": "api", "name": "lat", "labels": [],
                                 "values": [1], "count": 1}               # 统计字段不完整
        bad_payloads.append(partial)
        empty_stats = json.loads(t.json())
        empty_stats["samples"] = [{"service": "", "name": "e", "labels": [],
                                   "values": [], "count": 0, "sum": 0.0,
                                   "minimum": 0.0, "maximum": 0.0, "mean": 0.0}]
        bad_payloads.append(empty_stats)                                  # 空 values 带统计
        bad_labels = json.loads(t.json())
        bad_labels["counters"] = [{"service": "", "name": "a", "value": 1,
                                   "labels": [["k", 1], ["k", 2]]}]       # 标签重复键
        bad_payloads.append(bad_labels)
        unhashable_span = json.loads(t.json())
        unhashable_span["spans"] = [{"span": [1, 2], "service": "", "parent": None,
                                     "start": 0, "end": None, "error": None}]
        bad_payloads.append(unhashable_span)                              # 不可哈希跨度标识
        nan_value = json.loads(t.json())
        nan_value["samples"][0]["values"] = [float("nan")]                # 非有限样本值
        bad_payloads.append(nan_value)
        bad_service = json.loads(t.json())
        bad_service["counters"][0]["service"] = None                      # 非法服务
        bad_payloads.append(bad_service)
        for bad in bad_payloads:
            with self.assertRaises(ValueError, msg=repr(bad)):
                Telemetry.from_snapshot(bad)
        # 失败不影响已有实例
        self.assertEqual(t.snapshot(), good)

    def test_input_not_mutated(self):
        t = self.make_telemetry()
        snap = t.snapshot()
        frozen = json.loads(t.json())
        Telemetry.from_snapshot(snap)
        self.assertEqual(json.loads(json.dumps(snap, sort_keys=True)), frozen)


if __name__ == "__main__":
    unittest.main()
