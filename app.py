import json
import math
import time


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
    def _json_safe_error(error):
        # 能被标准 JSON 严格表示的原样保留；否则整体替换为
        # {"type": 运行时类名, "message": str(error)}。取值过程中的任何
        # 异常都不得泄漏给调用方，失败的部分回退为空字符串。
        try:
            json.dumps(error, allow_nan=False)
        except Exception:
            try:
                type_name = type(error).__name__
            except Exception:
                type_name = ""
            try:
                message = str(error)
            except Exception:
                message = ""
            return {"type": type_name, "message": message}
        return error

    def json(self):
        snapshot = self.snapshot()
        # 只转换本次序列化用的快照副本：span 条目由 snapshot() 新建，
        # 改写其中的 error 不会回写 self.spans，也不影响 query 结果。
        for entry in snapshot["spans"]:
            entry["error"] = self._json_safe_error(entry["error"])
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
