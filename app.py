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
