import copy
import json
import math
import time
from collections.abc import Mapping


class _Orderable:
    """排序包装：优先按自然序比较，类型不可比时回退到 repr 字符串，
    保证快照排序在混合类型标签/标识下也不会失败。"""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        a, b = self.value, other.value
        try:
            if a < b:
                return True
            if b < a:
                return False
        except TypeError:
            pass
        return repr(a) < repr(b)


class Telemetry:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.counters = {}
        self.samples = {}
        self.spans = {}

    @staticmethod
    def _service(service):
        # 缺省（None）归一化为空字符串表示默认服务；显式传入必须是非空字符串。
        if service is None:
            return ""
        if not isinstance(service, str) or service == "":
            raise ValueError("service must be a non-empty string")
        return service

    @staticmethod
    def _normalize_labels(labels):
        # 标签按键的字典序归一化；重复键或不可 JSON 序列化一律 ValueError，
        # 调用方在校验通过前不会写入任何聚合。
        try:
            pairs = []
            seen = set()
            for item in labels:
                key, value = item
                if key in seen:
                    raise ValueError("duplicate label key: %r" % (key,))
                seen.add(key)
                pairs.append((key, value))
            pairs.sort(key=lambda pair: pair[0])
            json.dumps(pairs, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid labels: %s" % (exc,))
        return tuple(pairs)

    def inc(self, name, value=1, labels=(), service=None):
        service = self._service(service)
        labels = self._normalize_labels(labels)
        key = (service, name, labels)
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name, value, labels=(), service=None):
        service = self._service(service)
        labels = self._normalize_labels(labels)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError("sample value must be numeric")
        if math.isnan(numeric) or math.isinf(numeric):
            raise ValueError("sample value must be finite, got %r" % (value,))
        key = (service, name, labels)
        self.samples.setdefault(key, []).append(value)

    def start(self, span, parent=None, service=None):
        service = self._service(service)
        self.spans[(service, span)] = {
            "parent": parent,
            "start": self.clock(),
            "end": None,
            "error": None,
        }

    def finish(self, span, error=None, service=None):
        service = self._service(service)
        record = self.spans[(service, span)]
        record["end"] = self.clock()
        record["error"] = error

    @staticmethod
    def _span_entry(service, span, record):
        return {
            "span": span,
            "service": service,
            "parent": record["parent"],
            "start": record["start"],
            "end": record["end"],
            "error": record["error"],
        }

    def _span_entries(self):
        entries = [
            self._span_entry(service, span, record)
            for (service, span), record in self.spans.items()
        ]
        entries.sort(
            key=lambda entry: (
                _Orderable(entry["service"]),
                _Orderable(entry["start"]),
                _Orderable(entry["span"]),
            )
        )
        return entries

    def query(self, status):
        # open：end 仍为空；error：已结束且 error 非空。其他状态一律 ValueError。
        if status not in ("open", "error"):
            raise ValueError("status must be 'open' or 'error'")
        result = []
        for entry in self._span_entries():
            if status == "open":
                if entry["end"] is None:
                    result.append(entry)
            elif entry["end"] is not None and entry["error"]:
                result.append(entry)
        return result

    @staticmethod
    def _sample_stats(values):
        floats = [float(value) for value in values]
        total = 0.0
        for number in floats:  # 按 values 写入顺序累加
            total += number
        count = len(floats)
        return {
            "count": count,
            "sum": total,
            "minimum": min(floats),
            "maximum": max(floats),
            "mean": total / count,
        }

    def snapshot(self):
        counters = []
        for (service, name, labels), value in sorted(
            self.counters.items(),
            key=lambda item: tuple(_Orderable(x) for x in item[0]),
        ):
            counters.append({
                "service": service,
                "name": name,
                "labels": list(labels),
                "value": value,
            })

        samples = []
        for (service, name, labels), values in sorted(
            self.samples.items(),
            key=lambda item: tuple(_Orderable(x) for x in item[0]),
        ):
            entry = {
                "service": service,
                "name": name,
                "labels": list(labels),
                "values": list(values),
            }
            if values:  # 空样本不产生统计
                entry.update(self._sample_stats(values))
            samples.append(entry)

        return {
            "counters": counters,
            "samples": samples,
            "spans": self._span_entries(),
        }

    @staticmethod
    def _is_json_strict(value):
        # 判定错误值能否被标准 JSON 严格表示：RFC 8259 不接受 NaN/Infinity
        # 和任意对象。以与最终输出一致的参数试编码；自定义对象在编码过程中
        # 再次抛出的任何异常都视为不可表示，绝不向 json 调用方泄漏。
        try:
            json.dumps(value, sort_keys=True, allow_nan=False)
        except Exception:
            return False
        return True

    @staticmethod
    def _error_placeholder(value):
        # 不可严格表示时，整个 error 替换为只含 type/message 的对象；
        # 取类名或 str() 失败则对应字符串退化为空字符串。
        try:
            type_name = type(value).__name__
        except Exception:
            type_name = ""
        try:
            message = str(value)
        except Exception:
            message = ""
        return {"type": type_name, "message": message}

    def json(self):
        snapshot = self.snapshot()
        # 只改写本次序列化所用的副本：snapshot() 每次新建字典，跨度条目
        # 需要替换 error 时再复制一份，绝不回写 self.spans / query 结果。
        safe_spans = []
        for entry in snapshot["spans"]:
            error = entry["error"]
            if self._is_json_strict(error):
                safe_spans.append(entry)  # 可表示：原样输出，类型与值不变
            else:
                safe_entry = dict(entry)
                safe_entry["error"] = self._error_placeholder(error)
                safe_spans.append(safe_entry)
        snapshot["spans"] = safe_spans
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))

    # ---- 离线快照恢复 ----

    _SAMPLE_STAT_FIELDS = ("count", "sum", "minimum", "maximum", "mean")

    @staticmethod
    def _require_json_strict(value, what):
        if not Telemetry._is_json_strict(value):
            raise ValueError("%s is not strictly JSON-representable" % (what,))

    @staticmethod
    def _require_hashable(value, what):
        try:
            hash(value)
        except Exception:
            raise ValueError("%s must be hashable" % (what,))

    @staticmethod
    def _loads_strict(text):
        # 与 json() 输出对偶的严格解析：拒绝重复键（标准 loads 会静默覆盖）
        # 以及 NaN/Infinity 这类非 RFC 8259 常量。
        def reject_constant(name):
            raise ValueError("invalid JSON constant: %s" % (name,))

        def object_hook(pairs):
            seen = set()
            obj = {}
            for key, value in pairs:
                if key in seen:
                    raise ValueError("duplicate key: %r" % (key,))
                seen.add(key)
                obj[key] = value
            return obj

        try:
            return json.loads(
                text, object_pairs_hook=object_hook, parse_constant=reject_constant
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("invalid JSON payload: %s" % (exc,))

    @classmethod
    def _restore_labels(cls, labels):
        if not isinstance(labels, (list, tuple)):
            raise ValueError("labels must be a list of pairs")
        # 复用写入路径的归一化：重复键或不可 JSON 序列化一律 ValueError。
        normalized = cls._normalize_labels(labels)
        cls._require_hashable(normalized, "labels")
        return normalized

    @classmethod
    def _metric_key(cls, record, required, optional):
        if not isinstance(record, Mapping):
            raise ValueError("record must be a JSON object")
        keys = set(record)
        allowed = set(required) | set(optional)
        if not required <= keys or not keys <= allowed:
            raise ValueError("record has missing or extra fields")
        service = record["service"]
        if not isinstance(service, str):
            raise ValueError("service must be a string")
        name = record["name"]
        cls._require_json_strict(name, "name")
        cls._require_hashable(name, "name")
        return service, name, cls._restore_labels(record["labels"])

    @staticmethod
    def _checked_sample_value(value):
        # 与 observe 相同的有限数值规则。
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError("sample value must be numeric")
        if math.isnan(numeric) or math.isinf(numeric):
            raise ValueError("sample value must be finite, got %r" % (value,))

    @classmethod
    def _restore_counters(cls, records):
        counters = {}
        for record in records:
            service, name, labels = cls._metric_key(
                record, {"service", "name", "labels", "value"}, ()
            )
            value = record["value"]
            cls._require_json_strict(value, "counter value")
            key = (service, name, labels)
            if key in counters:
                raise ValueError("duplicate counter record")
            counters[key] = copy.deepcopy(value)
        return counters

    @classmethod
    def _restore_samples(cls, records):
        samples = {}
        required = {"service", "name", "labels", "values"}
        for record in records:
            service, name, labels = cls._metric_key(
                record, required, cls._SAMPLE_STAT_FIELDS
            )
            values = record["values"]
            if not isinstance(values, list):
                raise ValueError("sample values must be a list")
            for value in values:
                cls._require_json_strict(value, "sample value")
                cls._checked_sample_value(value)
            present = [f for f in cls._SAMPLE_STAT_FIELDS if f in record]
            if present:
                # 统计字段要么完整出现并与重算一致，要么完全不出现；
                # 空 values 按公开规则本就不附加统计。
                if len(present) != len(cls._SAMPLE_STAT_FIELDS) or not values:
                    raise ValueError("sample stats must be complete and non-empty")
                expected = cls._sample_stats(values)
                for field in cls._SAMPLE_STAT_FIELDS:
                    cls._require_json_strict(record[field], "sample stat")
                    if record[field] != expected[field]:
                        raise ValueError("sample stats inconsistent with values")
            key = (service, name, labels)
            if key in samples:
                raise ValueError("duplicate sample record")
            samples[key] = [copy.deepcopy(value) for value in values]
        return samples

    @classmethod
    def _restore_spans(cls, records):
        spans = {}
        required = {"span", "service", "parent", "start", "end", "error"}
        for record in records:
            if not isinstance(record, Mapping) or set(record) != required:
                raise ValueError(
                    "span record must contain exactly %s" % (sorted(required),)
                )
            service = record["service"]
            if not isinstance(service, str):
                raise ValueError("service must be a string")
            span = record["span"]
            cls._require_json_strict(span, "span")
            cls._require_hashable(span, "span")
            # end 只能为空或已结束值；其余时间/父子/错误值同样必须可严格 JSON 表示。
            for field in ("parent", "start", "end", "error"):
                cls._require_json_strict(record[field], "span %s" % (field,))
            key = (service, span)
            if key in spans:
                raise ValueError("duplicate span record")
            spans[key] = {
                "parent": copy.deepcopy(record["parent"]),
                "start": copy.deepcopy(record["start"]),
                "end": copy.deepcopy(record["end"]),
                "error": copy.deepcopy(record["error"]),
            }
        return spans

    @classmethod
    def _validated_state(cls, data):
        if not isinstance(data, Mapping):
            raise ValueError("payload must be a snapshot object")
        try:
            keys = set(data)
        except Exception:
            raise ValueError("payload keys must be hashable")
        if keys != {"counters", "samples", "spans"}:
            raise ValueError("payload must contain exactly counters, samples and spans")
        sections = (data["counters"], data["samples"], data["spans"])
        if any(not isinstance(section, list) for section in sections):
            raise ValueError("counters, samples and spans must be arrays")
        # 全部校验在本地结构上完成后才建实例，失败不会留下半成品。
        counters = cls._restore_counters(sections[0])
        samples = cls._restore_samples(sections[1])
        spans = cls._restore_spans(sections[2])
        return counters, samples, spans

    @classmethod
    def from_snapshot(cls, payload):
        """从 snapshot() 返回的对象或 json() 返回的紧凑 JSON 文本重建独立的
        Telemetry 实例：计数器、样本原始 values 和跨度 parent/start/end/error
        全部带回，统计按 values 重算。输入不被修改，新实例与输入不共享列表
        或记录；任何无法恢复的内容统一抛 ValueError，且不产生半成品实例。"""
        if isinstance(payload, (str, bytes, bytearray)):
            data = cls._loads_strict(payload)
        elif isinstance(payload, Mapping):
            data = payload
        else:
            raise ValueError("payload must be a snapshot object or JSON text")
        counters, samples, spans = cls._validated_state(data)
        instance = cls()
        instance.counters = counters
        instance.samples = samples
        instance.spans = spans
        return instance
