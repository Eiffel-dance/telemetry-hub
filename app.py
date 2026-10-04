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


class _OrderableTuple:
    """逐元素按 _Orderable 规则做字典序比较的排序键。

    不能直接使用 (_Orderable(v), ...) 普通元组：元组比较先以相等性短路，
    而 _Orderable 未定义值相等语义，包装同一相等值的两个不同实例会被
    误判为不可区分，后续元素（start、span）的平局判定永远不会执行，
    同服务同开始时间的跨度只能退回插入顺序。这里仅依赖每个元素自己的
    __lt__，单元素比较语义与直接使用 _Orderable 完全一致。
    """

    __slots__ = ("parts",)

    def __init__(self, values):
        self.parts = tuple(_Orderable(value) for value in values)

    def __lt__(self, other):
        for left, right in zip(self.parts, other.parts):
            if left < right:
                return True
            if right < left:
                return False
        return False


class _FrozenKind:
    """冻结标签值时区分数组与对象的标记。标量原样保留，容器递归冻结为
    可哈希元组；标记提供稳定 repr，保证混合标签下快照排序仍然确定。"""

    __slots__ = ("kind",)

    def __init__(self, kind):
        self.kind = kind

    def __repr__(self):
        return "<frozen-%s>" % (self.kind,)


_FROZEN_ARRAY = _FrozenKind("array")
_FROZEN_OBJECT = _FrozenKind("object")


def _freeze_json(value):
    """把任意合法 JSON 值冻结为可哈希的规范形式。

    标量原样返回（既有可哈希标量标签的聚合键与输出保持不变）；数组
    （含元组）按顺序递归冻结，元素顺序仍然区分不同标签；对象按键排序
    后递归冻结，键顺序不影响聚合相等性。冻结只构造新结构，不改动
    调用方对象。键不可比较的混合类型对象在此抛出 TypeError，由
    _normalize_labels 统一转换为 ValueError。
    """
    if isinstance(value, (list, tuple)):
        return (_FROZEN_ARRAY, tuple(_freeze_json(item) for item in value))
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: item[0])
        return (
            _FROZEN_OBJECT,
            tuple((key, _freeze_json(item)) for key, item in items),
        )
    return value


def _thaw_json(value):
    """_freeze_json 的逆变换：从冻结形式重建原始 JSON 结构。

    每次调用都产生全新的数组/对象，返回值与内部冻结状态互不共享；
    标量原样返回。
    """
    if isinstance(value, tuple) and len(value) == 2:
        marker, payload = value
        if marker is _FROZEN_ARRAY:
            return [_thaw_json(item) for item in payload]
        if marker is _FROZEN_OBJECT:
            return {key: _thaw_json(item) for key, item in payload}
    return value


def _thaw_labels(labels):
    # 快照输出：按键序返回原始 JSON 结构，每项仍是 (键, 值) 元组，
    # 数组/对象值重建为全新对象，与内部冻结形式互不共享。
    return [(key, _thaw_json(value)) for key, value in labels]


def _copy_input_value(value):
    # start/finish 入站原值（parent/error）与只读结果之间的隔离：仅对可变
    # 容器做深拷贝，标量与自定义对象原样保留。容器只可能由合法 JSON 值构成
    # （恢复路径拒绝非严格 JSON 值），深拷贝不会失败；直接 start/finish 可能
    # 收到不可复制的容器，此时回退为原值——读取侧按同一规则再复制，既不为
    # 隔离新增拒绝条件，也绝不把半拷贝结构泄漏进返回结果。异常实例必须原样：
    # 异常按身份比较，拷贝会破坏“重复读取结果相等”这一既有可观察行为。
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        try:
            return copy.deepcopy(value)
        except Exception:
            return value
    return value


class SnapshotFormatError(ValueError):
    """快照恢复格式错误的公开异常类型。

    所有快照恢复入口（from_snapshot/merge_snapshot/diff_snapshots/restore/
    restore_snapshot）对不可解析的 JSON、非对象顶层、不受支持的版本、缺失
    或类型错误的字段、非有限数值、无法按既有规则规范化的标签、悬空或成环
    的父子引用、重复跨度标识等输入问题统一抛出本异常。它是 ValueError 的
    子类，既有按 ValueError 捕获的调用方行为不变。
    """


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
        # 标签按键的字典序归一化；重复键、键排序失败或不可严格 JSON
        # 表示一律 ValueError，调用方在校验通过前不会写入任何聚合。
        # 值可以是任意有限 JSON 标量、数组或对象：标量原样进入聚合键
        # （既有行为不变），数组/对象递归冻结为可哈希的规范形式——
        # 对象键顺序不造成差异，数组顺序仍然区分不同标签；冻结只构造
        # 新结构，不改写调用方对象。
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
            return tuple(
                (key, _freeze_json(value)) for key, value in pairs
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid labels: %s" % (exc,))

    def inc(self, name, value=1, labels=(), service=None):
        service = self._service(service)
        labels = self._normalize_labels(labels)
        self._apply_inc(self.counters, service, name, value, labels)

    def observe(self, name, value, labels=(), service=None):
        service = self._service(service)
        labels = self._normalize_labels(labels)
        self._check_sample_value(value)
        self._apply_observe(self.samples, service, name, value, labels)

    @staticmethod
    def _check_sample_value(value):
        # observe 的样本入站规则：可转 float 且必须有限，原值由调用方保留。
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError("sample value must be numeric")
        if math.isnan(numeric) or math.isinf(numeric):
            raise ValueError("sample value must be finite, got %r" % (value,))
        return value

    @staticmethod
    def _apply_inc(counters, service, name, value, labels):
        # 计数器写入的唯一实现点，批量提交与逐条 inc 共用：
        # 右值先算完再赋值，不可相加时原结构保持不变。
        key = (service, name, labels)
        counters[key] = counters.get(key, 0) + value

    @staticmethod
    def _apply_observe(samples, service, name, value, labels):
        # 样本追加的唯一实现点，原值与写入顺序由列表保留。
        key = (service, name, labels)
        samples.setdefault(key, []).append(value)

    @staticmethod
    def _apply_start(spans, service, span, parent, timestamper):
        # 跨度创建的唯一实现点：重复校验先于时间戳读取，
        # timestamper 每次创建只被调用一次。parent 中的可变容器在此复制，
        # 调用方事后修改入参对象不会改写已记录跨度。
        key = (service, span)
        if key in spans:
            raise ValueError(
                "span already exists for service=%r span=%r" % (service, span)
            )
        spans[key] = {
            "parent": _copy_input_value(parent),
            "start": timestamper(),
            "end": None,
            "error": None,
        }

    @staticmethod
    def _apply_finish(spans, service, span, error, timestamper):
        # 跨度结束的唯一实现点：不存在与已结束的校验先于时间戳读取，
        # timestamper 每次结束只被调用一次。error 中的可变容器在此复制，
        # 调用方事后修改入参对象不会改写已记录跨度。
        key = (service, span)
        record = spans.get(key)
        if record is None:
            raise ValueError(
                "span not found for service=%r span=%r" % (service, span)
            )
        if record["end"] is not None:  # 已结束跨度不能二次结束
            raise ValueError(
                "span already finished for service=%r span=%r" % (service, span)
            )
        record["end"] = timestamper()
        record["error"] = _copy_input_value(error)

    def start(self, span, parent=None, service=None):
        # 写入前先完成 service 与 span 的有效性检查：service 缺省归一化为
        # 空字符串，显式传入必须是非空字符串；span 必须可哈希。任一校验
        # 失败都不推进 clock、不留下半条记录。
        service = self._service(service)
        self._restore_hashable(span, "span")
        # 跨度由 service 与 span 共同唯一标识：无论同标识跨度仍未结束还是
        # 已经结束，重复开始一律拒绝，原有 parent/start/end/error 不被覆盖，
        # clock 也不被推进。
        self._apply_start(self.spans, service, span, parent, self.clock)

    def finish(self, span, error=None, service=None):
        # 与 start 相同的入站校验顺序；被拒绝的调用不读取或生成时间戳。
        service = self._service(service)
        self._restore_hashable(span, "span")
        self._apply_finish(self.spans, service, span, error, self.clock)

    # ------------------------------------------------------------------
    # 离线批量回放
    # ------------------------------------------------------------------

    # 每种 op 允许出现的字段（op 本身单独处理）；字段缺省时沿用公开入口
    # 的默认值，因此这里列的是“允许”而非“必须”，必需字段另见
    # _BATCH_REQUIRED。
    _BATCH_FIELDS = {
        "inc": {"name", "value", "labels", "service"},
        "observe": {"name", "value", "labels", "service"},
        "start": {"span", "parent", "service"},
        "finish": {"span", "error", "service"},
    }

    # 公开入口中没有默认值、必须显式给出的字段：inc 的 value 缺省为 1，
    # observe 的 value 是位置参数无默认，name/span 始终必需。
    _BATCH_REQUIRED = {
        "inc": {"name"},
        "observe": {"name", "value"},
        "start": {"span"},
        "finish": {"span"},
    }

    def batch(self, events):
        """按输入顺序原子提交一批离线事件，成功返回 None。

        events 必须是事件对象（dict）组成的列表或元组；每个事件以 op
        指定 inc/observe/start/finish，其余字段沿用对应公开入口的名称、
        默认值与校验规则，批次内后续事件可使用前面事件刚建立的跨度。
        成功后 snapshot/json/query/trace 的结果与按同一顺序逐条调用
        inc/observe/start/finish 完全一致，每个 start/finish 仍只读取
        一次 clock。空批次视为成功且不改变状态。

        原子性：先在影子状态上完成全部结构校验、入站校验与生命周期
        模拟（重复开始、结束不存在或已结束的跨度等），该阶段不读取
        clock、不接触 self 的聚合；任一事件不合法统一抛 ValueError。
        全部通过后才在状态副本上按计划提交并一次性发布，提交阶段
        clock 自身抛出的异常原样传播，counter/sample/span 与 clock
        配置保持调用前状态。传入的事件对象及其标签、值均不被修改。
        """
        if not isinstance(events, (list, tuple)):
            raise ValueError("events must be a list or tuple of event objects")
        if len(events) == 0:  # 空批次成功：不复制、不读 clock、不改变状态
            return None
        actions = self._batch_plan(events)
        # 提交在副本上进行，全部动作成功后才一次性发布；clock 异常时
        # 局部副本被丢弃，self 仍指向调用前的结构。inc/observe 与逐条
        # 调用一样不读 clock，因此 clock 调用次序与逐条序列完全相同。
        counters = dict(self.counters)
        samples = {key: list(values) for key, values in self.samples.items()}
        spans = {key: dict(record) for key, record in self.spans.items()}
        for action in actions:
            kind = action[0]
            if kind == "inc":
                _, service, name, value, labels = action
                self._apply_inc(counters, service, name, value, labels)
            elif kind == "observe":
                _, service, name, value, labels = action
                self._apply_observe(samples, service, name, value, labels)
            elif kind == "start":
                _, service, span, parent = action
                self._apply_start(spans, service, span, parent, self.clock)
            else:
                _, service, span, error = action
                self._apply_finish(spans, service, span, error, self.clock)
        self.counters = counters
        self.samples = samples
        self.spans = spans
        return None

    def _batch_plan(self, events):
        # 在影子状态上按顺序预演整批事件，产出与 self 无关的动作清单。
        # 这里绝不调用 clock：start/finish 使用非 None 的占位时间戳，
        # 以便“已结束跨度不能二次结束”等依赖 end 非空的判断照常生效。
        planned = object()
        counters = dict(self.counters)
        samples = {key: list(values) for key, values in self.samples.items()}
        spans = {key: dict(record) for key, record in self.spans.items()}
        actions = []
        for index, event in enumerate(events):
            try:
                if not isinstance(event, dict):
                    raise ValueError(
                        "event at index %d must be an event object" % index
                    )
                if "op" not in event:
                    raise ValueError(
                        "event at index %d is missing 'op'" % index
                    )
                op = event["op"]
                if op not in self._BATCH_FIELDS:
                    raise ValueError(
                        "event at index %d has unknown op: %r" % (index, op)
                    )
                fields = set(event)
                allowed = self._BATCH_FIELDS[op] | {"op"}
                unknown = fields - allowed
                if unknown:
                    raise ValueError(
                        "event at index %d has unknown fields for op %r: %r"
                        % (index, op, sorted(unknown, key=repr))
                    )
                missing = self._BATCH_REQUIRED[op] - fields
                if missing:
                    raise ValueError(
                        "event at index %d for op %r is missing required"
                        " fields: %r" % (index, op, sorted(missing, key=repr))
                    )
                if op == "inc":
                    # 校验顺序与 inc 一致：service、labels，随后在影子上相加；
                    # value 缺省为 1。影子相加同时充当 value 的运算校验
                    # （不可相加的事件在本阶段即被拒绝）。
                    service = self._service(event.get("service"))
                    labels = self._normalize_labels(event.get("labels", ()))
                    name = event["name"]
                    value = event.get("value", 1)
                    self._apply_inc(counters, service, name, value, labels)
                    actions.append(("inc", service, name, value, labels))
                elif op == "observe":
                    # 与 observe 一致：service、labels、有限数值检查，再追加。
                    service = self._service(event.get("service"))
                    labels = self._normalize_labels(event.get("labels", ()))
                    name = event["name"]
                    value = event["value"]
                    self._check_sample_value(value)
                    self._apply_observe(samples, service, name, value, labels)
                    actions.append(("observe", service, name, value, labels))
                elif op == "start":
                    # 与 start 一致：service、span 可哈希，再在影子上查重。
                    service = self._service(event.get("service"))
                    span = event["span"]
                    self._restore_hashable(span, "span")
                    parent = event.get("parent", None)
                    self._apply_start(
                        spans, service, span, parent, lambda: planned
                    )
                    actions.append(("start", service, span, parent))
                else:
                    # 与 finish 一致：service、span 可哈希，再查不存在/已结束。
                    service = self._service(event.get("service"))
                    span = event["span"]
                    self._restore_hashable(span, "span")
                    error = event.get("error", None)
                    self._apply_finish(
                        spans, service, span, error, lambda: planned
                    )
                    actions.append(("finish", service, span, error))
            except ValueError:
                raise
            except Exception as exc:
                # 不可哈希 op/name、计数器值不可相加、事件对象的键枚举
                # 异常等底层错误同样属于事件被拒绝，按批量契约统一为
                # ValueError（影子状态随异常丢弃）。
                raise ValueError(
                    "event at index %d is invalid: %s" % (index, exc)
                )
        return actions

    @staticmethod
    def _span_entry(service, span, record):
        # 跨度记录的唯一构造点（query/snapshot/diff 共用）：记录字典本身是
        # 新建对象，parent/error 再各取一份独立副本，使每条返回记录与内部
        # 状态、同次其他记录之间都不共享可变容器；标量、异常实例与不可复制
        # 对象按原值语义返回。
        return {
            "span": span,
            "service": service,
            "parent": _copy_input_value(record["parent"]),
            "start": record["start"],
            "end": record["end"],
            "error": _copy_input_value(record["error"]),
        }

    def _span_entries(self):
        # query 使用的无筛选跨度条目，保持既有排序；快照筛选与差异查询
        # 统一走 _span_snapshot_pairs。
        return [entry for _, entry in self._span_snapshot_pairs(self.spans)]

    @staticmethod
    def _span_snapshot_pairs(spans, service=None, status=None):
        # 跨度快照条目的唯一构造点：按服务、开始时间、标识稳定排序，
        # 返回 (service, span) 定位键与完整记录。service/status 筛选与
        # snapshot 原有筛选逐项一致，缺省（None）即不限制。
        pairs = [
            ((svc, span), Telemetry._span_entry(svc, span, record))
            for (svc, span), record in spans.items()
        ]
        pairs.sort(
            key=lambda pair: _OrderableTuple(
                (
                    pair[1]["service"],
                    pair[1]["start"],
                    pair[1]["span"],
                )
            )
        )
        if service is None and status is None:
            return pairs
        return [
            (key, entry)
            for key, entry in pairs
            if (service is None or entry["service"] == service)
            and Telemetry._span_matches_status(entry, status)
        ]

    def query(self, status):
        # open：end 仍为空；closed：end 已写入（成功结束与带异常结束都包含，
        # 不再看 error 真值）；error：已结束且 error 不为 None。任何非 None
        # 的结束 error 都算异常——0、False、空字符串、空列表、空字典等假值
        # 也不例外，只有 None（JSON 中为 null）表示正常结束。只接受这三个
        # 字符串，其他字符串、空值、非字符串一律 ValueError；拒绝发生在
        # 读取任何跨度之前，不调用 clock，也不产生部分结果。每条命中都通过
        # _span_entry 生成独立记录字典，调用方改写返回列表或记录字段不影响
        # counters/samples/spans；error 原值（含异常实例）原样保留。
        if status not in ("open", "error", "closed"):
            raise ValueError("status must be 'open', 'error' or 'closed'")
        result = []
        for entry in self._span_entries():
            if status == "open":
                if entry["end"] is None:
                    result.append(entry)
            elif status == "closed":
                if entry["end"] is not None:
                    result.append(entry)
            elif entry["end"] is not None and entry["error"] is not None:
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
            # _span_entry 已使节点记录及其 parent/error 与内部状态隔离，
            # children 为新建数组，无需再整体深拷贝（整体深拷贝反而会复制
            # 异常实例等原值对象，破坏原值语义）。
            node = self._span_entry(service, node_span, node_record)
            node["children"] = []
            children = [
                (child_span, self.spans[(service, child_span)])
                for child_span in children_by_parent.get(node_span, ())
            ]
            children.sort(
                key=lambda item: _OrderableTuple(
                    (service, item[1]["start"], item[0])
                )
            )
            for child_span, child_record in children:
                node["children"].append(build(child_span, child_record))
            return node

        return build(span, record)

    @staticmethod
    def _check_percentile_q(q):
        # 只接受非 bool 的 int/float：bool 是 int 的子类，必须显式排除；
        # 其他数值类型（Decimal、Fraction、字符串等）一律拒绝。
        if isinstance(q, bool) or not isinstance(q, (int, float)):
            raise ValueError("q must be an int or float, got %r" % (q,))
        if not math.isfinite(q):
            raise ValueError("q must be finite, got %r" % (q,))
        if q < 0 or q > 100:
            raise ValueError("q must be within [0, 100], got %r" % (q,))
        return q

    def percentile(self, name, q, labels=(), service=None):
        """离线诊断：对一条已记录的样本序列计算分位数，只读不改状态。

        service 与 labels 的归一化规则与 observe 完全一致（service 缺省为
        默认服务，显式传入必须是非空字符串；标签按键排序、拒绝重复键与
        不可序列化值），name 必须可哈希才能作为样本键。q 只接受非 bool 的
        int/float，必须有限且落在 [0, 100] 闭区间，否则统一抛 ValueError。
        任何拒绝都发生在读取样本之前：不读 clock、不改变计数器、样本、
        跨度或输入对象，也不产生部分结果。样本键不存在或序列为空返回
        None；命中时按公开浮点规则把每个值转换为数值并升序排序（排序
        副本不回写 values），以位置 (n-1)*q/100 线性插值，位置为整数时
        直接取该项，q=0/100 分别得到最小值/最大值，返回 Python float。
        只读取当前聚合，重复调用结果相同。
        """
        service = self._service(service)
        labels = self._normalize_labels(labels)
        self._restore_hashable(name, "name")
        q = self._check_percentile_q(q)
        values = self.samples.get((service, name, labels))
        if not values:  # 键不存在或空序列：无分位数可言
            return None
        ordered = sorted(float(value) for value in values)
        position = (len(ordered) - 1) * q / 100.0
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:  # 整数位置（含 q=0 与 q=100）：直接取该项
            return float(ordered[lower])
        fraction = position - lower
        return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)

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

    @staticmethod
    def _filter_service(service):
        # snapshot/json 的服务筛选：缺省（None）匹配全部服务；提供时只能是
        # 字符串，空字符串表示默认服务（与写入入口 _service 不同，那里显式
        # 空串非法，这里空串是合法的筛选值）；其他类型一律 ValueError。
        if service is not None and not isinstance(service, str):
            raise ValueError("service filter must be a string when provided")
        return service

    @staticmethod
    def _filter_status(status):
        # 跨度状态筛选：缺省（None）保留所有服务的跨度；提供时只接受
        # query 的同一组状态，拒绝规则也保持一致，非法值在读取聚合前抛出。
        if status is not None and status not in ("open", "error", "closed"):
            raise ValueError("status must be 'open', 'error' or 'closed'")
        return status

    @staticmethod
    def _span_matches_status(entry, status):
        # 沿用 query 对结束与异常的既有定义：open 看 end 为空；closed 看
        # end 已写入（成功与带异常结束都包含）；error 要求已结束且
        # error 不为 None（0、False、空容器等假值也不例外）。
        if status is None:
            return True
        if status == "open":
            return entry["end"] is None
        if status == "closed":
            return entry["end"] is not None
        return entry["end"] is not None and entry["error"] is not None

    def snapshot(self, service=None, labels=None, status=None):
        """导出稳定排序的只读快照，支持可选的离线诊断筛选。

        三个筛选全部可省略，省略时与无参数调用逐项一致，仍返回 counters、
        samples、spans 三个数组。service 缺省匹配全部服务，提供时必须是字符
        串，空字符串表示默认服务；labels 缺省匹配全部标签，提供时沿用 observe
        的成对输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合
        与计数器/样本精确匹配（显式传空序列只命中无标签记录）；status 缺省
        保留所有服务的跨度，提供时只能是 open/closed/error，判定与 query 完全
        相同。service 对三个数组同时生效，status 只作用于跨度，父标识不随筛选
        改写。任一筛选非法都在读取聚合前抛 ValueError：不调用 clock、不产生
        部分结果；无匹配时对应数组为空。统计只对命中的样本序列按原规则重算，
        排序仍按服务、名称、标签或跨度开始时间、标识的稳定顺序。返回的字典、
        数组与记录均为独立副本，重复使用同一组筛选结果相同，筛选不写入、清空
        或重排任何内部数据。
        """
        # 全部筛选先校验、归一化，之后才读取聚合，保证非法筛选不产生部分
        # 结果；整个过程纯只读，不调用 clock。
        service = self._filter_service(service)
        status = self._filter_status(status)
        if labels is not None:
            labels = self._normalize_labels(labels)

        counters = [
            entry
            for _, entry in self._counter_snapshot_pairs(
                self.counters, service, labels
            )
        ]
        samples = [
            entry
            for _, entry in self._sample_snapshot_pairs(
                self.samples, service, labels
            )
        ]
        spans = [
            entry
            for _, entry in self._span_snapshot_pairs(
                self.spans, service, status
            )
        ]

        return {
            "counters": counters,
            "samples": samples,
            "spans": spans,
        }

    @staticmethod
    def _counter_snapshot_pairs(counters, service=None, labels=None):
        # 计数器快照条目的唯一构造点：按服务、名称、标签稳定排序，
        # 返回 ((service, name, labels 冻结键), 完整记录)。service/labels
        # 筛选与 snapshot 原有筛选逐项一致，缺省（None）即不限制。
        pairs = []
        for (svc, name, key_labels), value in sorted(
            counters.items(),
            key=lambda item: _OrderableTuple(item[0]),
        ):
            if service is not None and svc != service:
                continue
            if labels is not None and key_labels != labels:
                continue
            pairs.append((
                (svc, name, key_labels),
                {
                    "service": svc,
                    "name": name,
                    "labels": _thaw_labels(key_labels),
                    "value": value,
                },
            ))
        return pairs

    @staticmethod
    def _sample_snapshot_pairs(samples, service=None, labels=None):
        # 样本快照条目的唯一构造点：按服务、名称、标签稳定排序，
        # 返回 ((service, name, labels 冻结键), 完整记录)。统计对非空
        # values 按原规则重算，空 values 不附加统计字段。
        pairs = []
        for (svc, name, key_labels), values in sorted(
            samples.items(),
            key=lambda item: _OrderableTuple(item[0]),
        ):
            if service is not None and svc != service:
                continue
            if labels is not None and key_labels != labels:
                continue
            entry = {
                "service": svc,
                "name": name,
                "labels": _thaw_labels(key_labels),
                "values": list(values),
            }
            if values:  # 空样本不产生统计；统计只对命中序列按原规则重算
                entry.update(Telemetry._sample_stats(values))
            pairs.append(((svc, name, key_labels), entry))
        return pairs

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

    def json(self, service=None, labels=None, status=None):
        # 筛选原样透传给 snapshot：非法筛选在 snapshot 的读取聚合前统一抛
        # ValueError，这里不产生部分序列化结果。可 JSON 表示的字段与同条件
        # snapshot 完全一致，error 仍按既有规则转换，紧凑表示与稳定键序不变。
        snapshot = self.snapshot(
            service=service, labels=labels, status=status
        )
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
        重复记录、统计不一致、无效标签、不可哈希标识等内容一律抛
        SnapshotFormatError（ValueError 的子类），且不会在抛出前留下半成品
        实例或改动任何已有实例。
        """
        data = cls._restore_parse(payload)
        counters, samples, spans = cls._restore_validate(data)
        instance = cls(clock)
        instance.counters = counters
        instance.samples = samples
        instance.spans = spans
        return instance

    # ------------------------------------------------------------------
    # 快照恢复与离线续采（严格入口）
    # ------------------------------------------------------------------

    @classmethod
    def _restore_strict_state(cls, payload):
        # restore/restore_snapshot 共用的解析与校验：版本、顶层结构、字段、
        # 统计一致性与父子引用全部通过后，返回全新的 counters/samples/spans；
        # 任何输入问题都是 SnapshotFormatError，且不产生任何半成品状态。
        data = cls._restore_parse(payload)
        counters, samples, spans = cls._restore_validate(
            data, allow_version=True
        )
        cls._restore_check_span_links(spans)
        return counters, samples, spans

    @staticmethod
    def _restore_check_span_links(spans):
        # 父子引用校验（仅严格恢复入口）：parent 为 None，或必须指向同一服务
        # 内已存在的跨度标识——与 trace 对父子关系的既定定义一致，指向其他
        # 服务或不存在的跨度（含不可哈希标识）都视为悬空引用。全部引用可解
        # 析后再沿父链检测有向环；每个跨度至多一个父引用，灰色重访即成环。
        for (service, span), record in spans.items():
            parent = record["parent"]
            if parent is None:
                continue
            try:
                key = (service, parent)
                hash(key)
            except TypeError:
                raise SnapshotFormatError(
                    "span parent does not reference an existing span: %r"
                    % (parent,)
                )
            if key not in spans:
                raise SnapshotFormatError(
                    "span parent does not reference an existing span: %r"
                    % (parent,)
                )
        color = {}  # 0 未访问 / 1 当前父链上 / 2 已确认无环
        for key in spans:
            node = key
            chain = []
            while node is not None and color.get(node, 0) == 0:
                color[node] = 1
                chain.append(node)
                parent = spans[node]["parent"]
                node = (node[0], parent) if parent is not None else None
            if node is not None and color.get(node) == 1:
                raise SnapshotFormatError(
                    "cycle detected in span parent references"
                )
            for item in chain:
                color[item] = 2

    @classmethod
    def restore(cls, payload, clock=time.time):
        """严格恢复一份快照为可继续记录的新实例（离线续采）。

        接受 snapshot() 字典、json() 文本或 UTF-8 字节；顶层在 counters、
        samples、spans 之外可携带可选的 version。version 只接受非 bool 整数
        且必须落在公开兼容版本集合内（缺省按版本 1 的既定规则读取），其余
        版本一律拒绝。校验在 from_snapshot 的既有规则之上增加父子引用检查：
        每个非空 parent 必须指向同一服务内已存在的跨度，且父子关系不得成环。
        任何输入问题统一抛 SnapshotFormatError，不会留下半成品实例；全程不
        联网、不读写文件、不修改输入。恢复后的实例保留服务与标签维度、计数
        值、样本原值与公开浮点统计、每个跨度的开始/结束状态、异常信息与父
        跨度关系，可继续用 inc/observe/start/finish 记录，随后生成的快照与
        从同一事件序列全量记录得到的快照字段值与稳定排序一致。
        """
        counters, samples, spans = cls._restore_strict_state(payload)
        instance = cls(clock)
        instance.counters = counters
        instance.samples = samples
        instance.spans = spans
        return instance

    def restore_snapshot(self, payload):
        """把一份快照原子恢复到当前实例（替换全部聚合状态），成功返回 None。

        输入接受范围与校验规则和 restore 完全一致（含版本与父子引用检查）。
        先完整解析、校验并构建全新状态，最后一次性替换 counters/samples/
        spans：任何 SnapshotFormatError 都使当前实例的聚合、跨度与时钟配置
        保持调用前状态，输入对象不被改写，恢复后的数据不与 payload 共享可
        变对象。空快照得到可继续使用的空聚合器。
        """
        counters, samples, spans = self._restore_strict_state(payload)
        self.counters = counters
        self.samples = samples
        self.spans = spans
        return None

    # ------------------------------------------------------------------
    # 离线分片合并
    # ------------------------------------------------------------------

    def merge_snapshot(self, payload):
        """把另一份离线分片快照原子合并进当前实例，成功返回 None。

        输入接受范围与 from_snapshot 一致（快照字典、JSON 文本、UTF-8 字节）。
        先完整解析并校验，再在临时结构上试合并，最后一次性提交：任何字段
        缺失/多余、重复记录、非严格 JSON、无效标签、不可用标识、非有限样本
        等格式问题抛 SnapshotFormatError，计数器不可相加或同键跨度不一致抛
        ValueError，当前实例的全部聚合、
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

    # ------------------------------------------------------------------
    # 离线快照差异
    # ------------------------------------------------------------------

    @classmethod
    def diff_snapshots(cls, before, after, service=None, labels=None, status=None):
        """只读比较两份离线快照，返回 added/removed/changed 差异字典。

        两个输入都接受 snapshot() 返回的字典、json() 产生的 JSON 文本或
        UTF-8 字节，按 from_snapshot/merge_snapshot 相同的严格 JSON、字段、
        重复记录、标签与可哈希标识规则解析，任一输入无效统一抛
        SnapshotFormatError（ValueError 的子类），
        且在抛出前不返回任何结果、不修改输入对象。查询纯只读：不读取 clock、
        不联网、不改变任何输入。

        service/labels/status 是可选的同口径筛选，语义与 snapshot 完全一致：
        全部省略时与两参数调用逐项一致；service 缺省匹配全部服务，提供时
        只能是字符串（空字符串表示默认服务）；labels 缺省匹配全部标签，
        提供时沿用 observe 的成对输入、键排序、重复键与严格 JSON 校验，
        按归一化后的完整标签集合与计数器/样本精确匹配，不作用于跨度；
        status 缺省匹配全部跨度，提供时只接受 open/closed/error 且只作用
        于跨度，计数器与样本仍按原规则参与比较。任一筛选非法都在解析输入
        前抛 ValueError。before 与 after 各自先完成完整快照解析与严格校验，
        再独立应用同一组筛选，最后按既有定位键生成差异：筛选导致某条跨度
        只在一侧可见时，按筛选后的视图判定 added 或 removed。

        返回全新的、可 JSON 序列化的字典，顶层只有 counters、samples、spans，
        每一项都只含 added、removed、changed 三个数组。counter/sample 以
        service、name、labels 的完整组合定位，span 以 service、span 定位：
        只出现在 after 的完整记录进入 added，只出现在 before 的进入 removed，
        同一定位但内容不同的进入 changed（元素为 {"before": ..., "after": ...}
        两份相互独立的完整记录）。样本先按 values 与公开浮点统计重新归一再
        比较，输入携带或省略等价统计字段不算变化；跨度 open→closed、error
        变化、parent 变化等任何字段差异都算 changed。added/removed 沿用各
        自快照的稳定排序，changed 按 after 记录的快照排序；无差异对应数组
        为空。返回的记录与嵌套标签均可安全修改，与两个输入互不共享。
        """
        # 筛选先校验、归一化，之后才解析输入，保证非法筛选不产生部分结果；
        # 整个过程纯只读，不调用 clock。
        service = cls._filter_service(service)
        status = cls._filter_status(status)
        if labels is not None:
            labels = cls._normalize_labels(labels)

        before_data = cls._restore_parse(before)
        # 两边先全部解析并校验完成，之后才构造任何差异输出：任一输入无效
        # 都不会返回部分结果。
        after_data = cls._restore_parse(after)
        before_state = cls._restore_validate(before_data)
        after_state = cls._restore_validate(after_data)

        def diff_section(before_pairs, after_pairs):
            before_map = dict(before_pairs)
            after_map = dict(after_pairs)
            added = [
                entry
                for key, entry in after_pairs
                if key not in before_map
            ]
            removed = [
                entry
                for key, entry in before_pairs
                if key not in after_map
            ]
            changed = []
            for key, after_entry in after_pairs:
                before_entry = before_map.get(key)
                if before_entry is None:
                    continue
                if before_entry != after_entry:
                    changed.append(
                        {"before": before_entry, "after": after_entry}
                    )
            return {
                "added": added,
                "removed": removed,
                "changed": changed,
            }

        before_counters, before_samples, before_spans = before_state
        after_counters, after_samples, after_spans = after_state
        # 两侧在校验后的完整状态上独立应用同一组筛选：service 对三个分区
        # 同时生效，labels 只作用于计数器/样本，status 只作用于跨度。
        result = {
            "counters": diff_section(
                cls._counter_snapshot_pairs(before_counters, service, labels),
                cls._counter_snapshot_pairs(after_counters, service, labels),
            ),
            "samples": diff_section(
                cls._sample_snapshot_pairs(before_samples, service, labels),
                cls._sample_snapshot_pairs(after_samples, service, labels),
            ),
            "spans": diff_section(
                cls._span_snapshot_pairs(before_spans, service, status),
                cls._span_snapshot_pairs(after_spans, service, status),
            ),
        }
        # 再深拷贝一次切断与解析中间结构的引用（条目本身已是新建对象，
        # 这里保证容器层级同样全新独立，且结果严格可 JSON 序列化）。
        return copy.deepcopy(result)

    @staticmethod
    def _restore_pairs_hook(pairs):
        # object_pairs_hook：JSON 文本层面的重复键一律拒绝。
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise SnapshotFormatError(
                    "duplicate key in JSON object: %r" % (key,)
                )
            obj[key] = value
        return obj

    @staticmethod
    def _restore_constant(value):
        # parse_constant：NaN/Infinity 不是严格 JSON。
        raise SnapshotFormatError("non-strict JSON constant: %s" % (value,))

    @classmethod
    def _restore_parse(cls, payload):
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, (bytes, bytearray)):
            try:
                text = bytes(payload).decode("utf-8")
            except Exception as exc:
                raise SnapshotFormatError(
                    "payload bytes are not valid UTF-8: %s" % (exc,)
                )
        elif isinstance(payload, dict):
            # 深拷贝后再读取，保证新实例与原对象的列表、记录互不共享。
            try:
                return copy.deepcopy(payload)
            except Exception as exc:
                raise SnapshotFormatError(
                    "payload cannot be restored: %s" % (exc,)
                )
        else:
            raise SnapshotFormatError(
                "payload must be a snapshot dict or JSON text"
            )
        try:
            return json.loads(
                text,
                object_pairs_hook=cls._restore_pairs_hook,
                parse_constant=cls._restore_constant,
            )
        except SnapshotFormatError:
            raise
        except Exception as exc:
            # 含 JSONDecodeError：不可解析的文本统一归为快照格式错误。
            raise SnapshotFormatError("invalid JSON payload: %s" % (exc,))

    # 快照格式的当前公开版本与可兼容读取的旧版本集合；未携带 version 的
    # 既有快照（snapshot()/json() 的输出）按版本 1 的既定规则读取。
    SNAPSHOT_VERSION = 1
    _SNAPSHOT_COMPATIBLE_VERSIONS = frozenset({1})

    @classmethod
    def _restore_version(cls, version):
        # 版本只接受非 bool 的整数；不在兼容集合内的版本一律拒绝，
        # 兼容集合内的旧版本按既定规则读取（当前即版本 1 规则本身）。
        if isinstance(version, bool) or not isinstance(version, int):
            raise SnapshotFormatError(
                "snapshot version must be an integer, got %r" % (version,)
            )
        if version not in cls._SNAPSHOT_COMPATIBLE_VERSIONS:
            raise SnapshotFormatError(
                "unsupported snapshot version: %r" % (version,)
            )

    @classmethod
    def _restore_validate(cls, data, allow_version=False):
        if not isinstance(data, dict):
            raise SnapshotFormatError("payload must decode to a JSON object")
        required = {"counters", "samples", "spans"}
        allowed = set(required)
        if allow_version:
            allowed.add("version")
        keys = set(data)
        if not required <= keys or not keys <= allowed:
            raise SnapshotFormatError(
                "payload must contain exactly counters, samples and spans"
                + (" with optional version" if allow_version else "")
            )
        if "version" in data:
            cls._restore_version(data["version"])
        for section in ("counters", "samples", "spans"):
            if not isinstance(data[section], list):
                raise SnapshotFormatError("%s must be an array" % (section,))
        return (
            cls._restore_counters(data["counters"]),
            cls._restore_samples(data["samples"]),
            cls._restore_spans(data["spans"]),
        )

    @staticmethod
    def _restore_json_strict(value, what):
        if not Telemetry._is_json_strict(value):
            raise SnapshotFormatError(
                "%s is not strictly JSON-representable" % (what,)
            )
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
            raise SnapshotFormatError("service must be a string")
        return service

    @classmethod
    def _restore_name(cls, name):
        cls._restore_json_strict(name, "name")
        try:
            cls._restore_hashable(name, "name")
        except ValueError as exc:
            raise SnapshotFormatError(str(exc))
        return name

    @classmethod
    def _restore_labels(cls, labels):
        if not isinstance(labels, (list, tuple)):
            raise SnapshotFormatError("labels must be an array of pairs")
        for item in labels:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise SnapshotFormatError("labels must be an array of pairs")
        # 复用既有归一化：排序、重复键与严格 JSON 校验一致；数组/对象值
        # 冻结为可哈希规范形式后与写入路径使用同一聚合键。
        try:
            return cls._normalize_labels(labels)
        except ValueError as exc:
            raise SnapshotFormatError(str(exc))

    @staticmethod
    def _restore_sample_value(value):
        # 与 observe 相同的有限数值规则，原值保留。
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise SnapshotFormatError("sample value must be numeric")
        if math.isnan(numeric) or math.isinf(numeric):
            raise SnapshotFormatError(
                "sample value must be finite, got %r" % (value,)
            )
        return value

    @classmethod
    def _restore_counters(cls, records):
        counters = {}
        fields = {"service", "name", "labels", "value"}
        for record in records:
            if not isinstance(record, dict) or set(record) != fields:
                raise SnapshotFormatError(
                    "counter record must have exactly service, name, labels, value"
                )
            service = cls._restore_service(record["service"])
            name = cls._restore_name(record["name"])
            labels = cls._restore_labels(record["labels"])
            value = cls._restore_json_strict(record["value"], "counter value")
            key = (service, name, labels)
            if key in counters:
                raise SnapshotFormatError("duplicate counter record")
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
                raise SnapshotFormatError(
                    "sample record must have service, name, labels, values"
                    " and optionally stats"
                )
            service = cls._restore_service(record["service"])
            name = cls._restore_name(record["name"])
            labels = cls._restore_labels(record["labels"])
            values = record["values"]
            if not isinstance(values, list):
                raise SnapshotFormatError("sample values must be an array")
            checked = []
            for value in values:
                cls._restore_json_strict(value, "sample value")
                checked.append(cls._restore_sample_value(value))
            key = (service, name, labels)
            if key in samples:
                raise SnapshotFormatError("duplicate sample record")
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
                            raise SnapshotFormatError(
                                "sample stats inconsistent with values"
                            )
            elif keys & set(cls._STATS_KEYS):
                # 空 values 不附加统计字段。
                raise SnapshotFormatError(
                    "empty sample values must not carry stats"
                )
            samples[key] = checked
        return samples

    @classmethod
    def _restore_spans(cls, records):
        spans = {}
        fields = {"span", "service", "parent", "start", "end", "error"}
        for record in records:
            if not isinstance(record, dict) or set(record) != fields:
                raise SnapshotFormatError(
                    "span record must have exactly"
                    " span, service, parent, start, end, error"
                )
            service = cls._restore_service(record["service"])
            span = cls._restore_json_strict(record["span"], "span")
            try:
                cls._restore_hashable(span, "span")
            except ValueError as exc:
                raise SnapshotFormatError(str(exc))
            parent = cls._restore_json_strict(record["parent"], "parent")
            start = cls._restore_json_strict(record["start"], "start")
            end = record["end"]
            if end is not None:  # 结束时间只能为空或已结束值
                cls._restore_json_strict(end, "end")
            error = cls._restore_json_strict(record["error"], "error")
            key = (service, span)
            if key in spans:
                raise SnapshotFormatError("duplicate span record")
            spans[key] = {
                "parent": parent,
                "start": start,
                "end": end,
                "error": error,
            }
        return spans
