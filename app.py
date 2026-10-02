import copy
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

    def trace(self, span, service=None):
        """离线诊断：从指定跨度还原同一服务内的父子树。

        service 归一化规则与 start/finish 一致（缺省为默认服务，显式传入
        必须是非空字符串），跨度标识不可哈希时抛 ValueError。根跨度不存在
        返回 None；存在时返回独立树对象，节点含 span/service/parent/start/
        end/error 与 children 数组，children 递归包含 parent 与当前节点标识
        精确相等且 service 相同的直接子跨度，每层按快照的服务、开始时间、
        标识顺序排序，无子节点为空数组。父标识指向其他服务或不存在的跨度
        按无子节点处理；从根可达的父子引用构成环时抛 ValueError，不返回
        部分树。返回对象及其列表与聚合器互不共享，查询不改动任何已有数据。
        """
        service = self._service(service)
        self._restore_hashable(span, "span")
        record = self.spans.get((service, span))
        if record is None:
            return None

        # 预索引同一服务内 parent -> 子跨度标识，避免每层全表扫描；
        # 其他服务的跨度即使父标识相同也不属于本树。
        children_by_parent = {}
        for (child_service, child_span), child_record in self.spans.items():
            if child_service != service:
                continue
            parent = child_record["parent"]
            try:  # 不可哈希的父标识无法作为键，也就无法精确等于任何跨度键
                children_by_parent.setdefault(parent, []).append(child_span)
            except TypeError:
                continue

        visited = set()

        def build(node_span, node_record):
            key = (service, node_span)
            if key in visited:  # 每个跨度只有一个父标识，重复到达即成环
                raise ValueError("cycle detected in span parent references")
            visited.add(key)
            node = self._span_entry(service, node_span, node_record)
            node = copy.deepcopy(node)  # 与聚合器切断一切可变对象共享
            node["children"] = []
            children = [
                (child_span, self.spans[(service, child_span)])
                for child_span in children_by_parent.get(node_span, ())
            ]
            children.sort(
                key=lambda item: (
                    _Orderable(service),
                    _Orderable(item[1]["start"]),
                    _Orderable(item[0]),
                )
            )
            for child_span, child_record in children:
                node["children"].append(build(child_span, child_record))
            return node

        return build(span, record)

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

    # ------------------------------------------------------------------
    # 离线快照恢复
    # ------------------------------------------------------------------

    _STATS_KEYS = ("count", "sum", "minimum", "maximum", "mean")

    @classmethod
    def from_snapshot(cls, payload, clock=time.time):
        """把 snapshot() 字典或 json() 文本重建为独立的 Telemetry 实例。

        全程不联网、不读写文件、不修改输入；任何缺失/多余字段、非法 JSON、
        重复记录、统计不一致、无效标签、不可哈希标识等内容一律抛 ValueError，
        且不会在抛出前留下半成品实例或改动任何已有实例。
        """
        data = cls._restore_parse(payload)
        counters, samples, spans = cls._restore_validate(data)
        instance = cls(clock)
        instance.counters = counters
        instance.samples = samples
        instance.spans = spans
        return instance

    # ------------------------------------------------------------------
    # 离线分片合并
    # ------------------------------------------------------------------

    def merge_snapshot(self, payload):
        """把另一份离线分片快照原子合并进当前实例，成功返回 None。

        输入接受范围与 from_snapshot 一致（快照字典、JSON 文本、UTF-8 字节）。
        先完整解析并校验，再在临时结构上试合并，最后一次性提交：任何字段
        缺失/多余、重复记录、非严格 JSON、无效标签、不可用标识、非有限样本、
        计数器不可相加或同键跨度不一致都抛 ValueError，当前实例的全部聚合、
        跨度与时钟配置保持不变，输入对象也不被改写；解析阶段已切断与
        payload 的引用，合并后的数据不与 payload 或其中的列表、标签共享
        可变对象。
        """
        data = self._restore_parse(payload)
        counters, samples, spans = self._restore_validate(data)

        # 以下全部在副本上试合并，结束前绝不写回 self。
        merged_counters = dict(self.counters)
        for key, value in counters.items():
            if key in merged_counters:
                try:  # 与 inc 相同的加法语义：当前值在前、输入值在后
                    value = merged_counters[key] + value
                except Exception as exc:
                    raise ValueError(
                        "counter values cannot be added: %s" % (exc,)
                    )
            merged_counters[key] = value

        merged_samples = dict(self.samples)
        for key, values in samples.items():
            if key in merged_samples:
                # 新建列表：先保留当前 values，再按输入顺序追加，
                # 原值类型与写入顺序不变；统计在 snapshot 时统一重算。
                values = merged_samples[key] + values
            merged_samples[key] = values

        merged_spans = dict(self.spans)
        for key, record in spans.items():
            if key in merged_spans:
                try:  # 两方记录完全一致才幂等，不猜测生命周期如何更新
                    conflict = merged_spans[key] != record
                except Exception as exc:
                    raise ValueError(
                        "span records cannot be compared: %s" % (exc,)
                    )
                if conflict:
                    raise ValueError(
                        "conflicting span record for service=%r span=%r"
                        % (key[0], key[1])
                    )
            else:
                merged_spans[key] = record

        self.counters = merged_counters
        self.samples = merged_samples
        self.spans = merged_spans
        return None

    @staticmethod
    def _restore_pairs_hook(pairs):
        # object_pairs_hook：JSON 文本层面的重复键一律拒绝。
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate key in JSON object: %r" % (key,))
            obj[key] = value
        return obj

    @staticmethod
    def _restore_constant(value):
        # parse_constant：NaN/Infinity 不是严格 JSON。
        raise ValueError("non-strict JSON constant: %s" % (value,))

    @classmethod
    def _restore_parse(cls, payload):
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, (bytes, bytearray)):
            try:
                text = bytes(payload).decode("utf-8")
            except Exception as exc:
                raise ValueError("payload bytes are not valid UTF-8: %s" % (exc,))
        elif isinstance(payload, dict):
            # 深拷贝后再读取，保证新实例与原对象的列表、记录互不共享。
            try:
                return copy.deepcopy(payload)
            except Exception as exc:
                raise ValueError("payload cannot be restored: %s" % (exc,))
        else:
            raise ValueError("payload must be a snapshot dict or JSON text")
        try:
            return json.loads(
                text,
                object_pairs_hook=cls._restore_pairs_hook,
                parse_constant=cls._restore_constant,
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("invalid JSON payload: %s" % (exc,))

    @classmethod
    def _restore_validate(cls, data):
        if not isinstance(data, dict):
            raise ValueError("payload must decode to a JSON object")
        if set(data) != {"counters", "samples", "spans"}:
            raise ValueError(
                "payload must contain exactly counters, samples and spans"
            )
        for section in ("counters", "samples", "spans"):
            if not isinstance(data[section], list):
                raise ValueError("%s must be an array" % (section,))
        return (
            cls._restore_counters(data["counters"]),
            cls._restore_samples(data["samples"]),
            cls._restore_spans(data["spans"]),
        )

    @staticmethod
    def _restore_json_strict(value, what):
        if not Telemetry._is_json_strict(value):
            raise ValueError("%s is not strictly JSON-representable" % (what,))
        return value

    @staticmethod
    def _restore_hashable(value, what):
        try:
            hash(value)
        except TypeError:
            raise ValueError("%s must be hashable" % (what,))
        return value

    @staticmethod
    def _restore_service(service):
        # 快照中的服务已归一化：默认服务为空字符串，其余为非空字符串。
        if not isinstance(service, str):
            raise ValueError("service must be a string")
        return service

    @classmethod
    def _restore_name(cls, name):
        cls._restore_json_strict(name, "name")
        cls._restore_hashable(name, "name")
        return name

    @classmethod
    def _restore_labels(cls, labels):
        if not isinstance(labels, (list, tuple)):
            raise ValueError("labels must be an array of pairs")
        for item in labels:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("labels must be an array of pairs")
        # 复用既有归一化：排序、重复键与可 JSON 序列化校验一致。
        normalized = cls._normalize_labels(labels)
        cls._restore_hashable(normalized, "labels")
        return normalized

    @staticmethod
    def _restore_sample_value(value):
        # 与 observe 相同的有限数值规则，原值保留。
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError("sample value must be numeric")
        if math.isnan(numeric) or math.isinf(numeric):
            raise ValueError("sample value must be finite, got %r" % (value,))
        return value

    @classmethod
    def _restore_counters(cls, records):
        counters = {}
        fields = {"service", "name", "labels", "value"}
        for record in records:
            if not isinstance(record, dict) or set(record) != fields:
                raise ValueError(
                    "counter record must have exactly service, name, labels, value"
                )
            service = cls._restore_service(record["service"])
            name = cls._restore_name(record["name"])
            labels = cls._restore_labels(record["labels"])
            value = cls._restore_json_strict(record["value"], "counter value")
            key = (service, name, labels)
            if key in counters:
                raise ValueError("duplicate counter record")
            counters[key] = value
        return counters

    @classmethod
    def _restore_samples(cls, records):
        samples = {}
        required = {"service", "name", "labels", "values"}
        allowed = required | set(cls._STATS_KEYS)
        for record in records:
            keys = set(record) if isinstance(record, dict) else set()
            if not isinstance(record, dict) or not required <= keys or not keys <= allowed:
                raise ValueError(
                    "sample record must have service, name, labels, values"
                    " and optionally stats"
                )
            service = cls._restore_service(record["service"])
            name = cls._restore_name(record["name"])
            labels = cls._restore_labels(record["labels"])
            values = record["values"]
            if not isinstance(values, list):
                raise ValueError("sample values must be an array")
            checked = []
            for value in values:
                cls._restore_json_strict(value, "sample value")
                checked.append(cls._restore_sample_value(value))
            key = (service, name, labels)
            if key in samples:
                raise ValueError("duplicate sample record")
            if checked:
                # 统计一律按公开浮点规则从 values 重算；输入中存在的
                # 统计字段必须与重算结果一致。
                stats = cls._sample_stats(checked)
                for stat_key in cls._STATS_KEYS:
                    if stat_key in record:
                        given = cls._restore_json_strict(
                            record[stat_key], "sample stat"
                        )
                        if given != stats[stat_key]:
                            raise ValueError(
                                "sample stats inconsistent with values"
                            )
            elif keys & set(cls._STATS_KEYS):
                # 空 values 不附加统计字段。
                raise ValueError("empty sample values must not carry stats")
            samples[key] = checked
        return samples

    @classmethod
    def _restore_spans(cls, records):
        spans = {}
        fields = {"span", "service", "parent", "start", "end", "error"}
        for record in records:
            if not isinstance(record, dict) or set(record) != fields:
                raise ValueError(
                    "span record must have exactly"
                    " span, service, parent, start, end, error"
                )
            service = cls._restore_service(record["service"])
            span = cls._restore_json_strict(record["span"], "span")
            cls._restore_hashable(span, "span")
            parent = cls._restore_json_strict(record["parent"], "parent")
            start = cls._restore_json_strict(record["start"], "start")
            end = record["end"]
            if end is not None:  # 结束时间只能为空或已结束值
                cls._restore_json_strict(end, "end")
            error = cls._restore_json_strict(record["error"], "error")
            key = (service, span)
            if key in spans:
                raise ValueError("duplicate span record")
            spans[key] = {
                "parent": parent,
                "start": start,
                "end": end,
                "error": error,
            }
        return spans
