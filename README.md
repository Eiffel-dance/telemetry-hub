# Telemetry Hub

A dependency-free Python reference implementation for observability, metrics, tracing.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个轻量级本地遥测聚合器，统一记录计数器、数值样本和带父子关系的跨度，并输出稳定排序的 JSON 快照。聚合必须区分服务和标签，浮点统计采用公开规则，未结束跨度和异常状态要可查询；整个组件不发送网络请求，快照可直接用于离线诊断和回放。

## API

- `inc(name, value=1, labels=(), service=None)` / `observe(name, value, labels=(), service=None)`：可选 `service`，缺省归一化为空字符串（默认服务），显式服务名必须是非空字符串；同名同标签但服务不同分别聚合。标签按键字典序归一化，重复键或不可 JSON 序列化的标签抛 `ValueError`，且不改变已有聚合。
- 样本保留按写入顺序的原值 `values`；快照对非空样本附 `count`、`sum`（按写入顺序累加）、`minimum`、`maximum`、`mean`（`sum/count`，不四舍五入）。NaN/无穷值抛 `ValueError`，空样本不产生统计。
- `start(span, parent=None, service=None)` / `finish(span, error=None, service=None)`：service 与跨度标识共同定位跨度；父标识原样保留。`start` 写入前先完成 service 与 span 校验（service 缺省归一化为空字符串，显式传入必须是非空字符串；span 必须可哈希），相同 service/span 标识已存在跨度时——无论仍未结束还是已经结束——都抛 `ValueError`，不覆盖原有 parent/start/end/error，也不推进 clock；首次创建成功时以一次 clock 结果作为 `start`，返回 `None`。`finish` 只结束当前存在且 `end` 仍为空的跨度：标识不存在或跨度已结束时抛 `ValueError`，不改变任何聚合数据、不生成新的时间戳；有效调用把 `end` 设为一次 clock 结果、把 `error` 设为传入值并返回 `None`。start/finish 收到不可哈希 span 一律抛 `ValueError`，所有被拒绝的调用都不留下半条记录，也不影响其他服务的数据。
- `query(status)`：仅接受 `'open'`（`end` 为空）、`'closed'`（`end` 已写入，成功结束与带异常结束都算）和 `'error'`（已结束且 `error` 非空），其他字符串、空值或非字符串值抛 `ValueError`（拒绝时不调用 clock、不留部分结果），无匹配返回 `[]`；结果含 `span/service/parent/start/end/error`，按服务、开始时间、标识排序；返回值为独立列表与独立记录，调用方修改不影响聚合器，`error` 原值（含异常对象）保留。
- `trace(span, service=None)`：离线诊断用跨度树查询。service 归一化规则与 `start`/`finish` 一致，跨度标识不可哈希抛 `ValueError`；根跨度不存在返回 `None`，存在时返回独立树对象，节点含 `span/service/parent/start/end/error` 与 `children` 数组，`children` 递归包含 parent 与当前节点标识精确相等且 service 相同的直接子跨度，每层按服务、开始时间、标识排序，无子节点为空数组。父标识指向其他服务或不存在的跨度按无子节点处理；从根可达的父子引用构成环时抛 `ValueError`，不返回部分树。返回对象及其列表与聚合器互不共享，查询不改动任何已有数据。
- `percentile(name, q, labels=(), service=None)`：只读的分位数查询，用于离线诊断样本分布，调用方不需要先导出或修改聚合器状态。`service` 与 `labels` 的归一化规则与 `observe` 完全一致（`service` 缺省归一化为空字符串，显式传入必须是非空字符串；标签按键字典序归一化，重复键或不可 JSON 序列化抛 `ValueError`），`name` 必须可哈希才能作为样本键，不可哈希抛 `ValueError`。`q` 只接受非 `bool` 的 `int`/`float`，必须为有限值且落在 `[0, 100]` 闭区间，否则统一抛 `ValueError`；任何拒绝都发生在读取样本之前——不读 clock、不改变计数器、样本、跨度或输入对象，也不产生部分结果。样本键不存在或序列为空返回 `None`；命中时按公开浮点规则把每个值转换为数值并升序排序（排序副本不回写 `values`），以位置 `(n-1)*q/100` 线性插值，位置为整数时直接取该项，`q=0`/`q=100` 分别得到最小值/最大值，返回 Python `float`。该入口只读取当前聚合，重复调用结果相同，也不新增 `snapshot()`/`json()` 字段。
- `snapshot(service=None, labels=None, status=None)` / `json(service=None, labels=None, status=None)`：计数器和样本按服务、名称、标签排序，跨度按服务、开始时间、标识排序；JSON 紧凑（`separators=(",", ":")`）且键序稳定（`sort_keys=True`）。三个筛选全部可省略，省略任一筛选即不按该维度限制，无参数调用结果与既有行为逐项一致，仍返回 `counters`、`samples`、`spans` 三个数组。`service` 缺省匹配全部服务，提供时只能是字符串，空字符串表示默认服务，其他类型抛 `ValueError`；`labels` 缺省匹配全部标签，提供时沿用 `observe` 的成对标签输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合与计数器/样本精确匹配（显式空序列只命中无标签记录），标签值无法通过既有规则时统一抛 `ValueError`；`status` 缺省保留所有服务的跨度，提供时只能是 `open`/`closed`/`error` 并使用 `query` 对结束与异常的既有定义。`service` 对三个数组同时生效，`status` 只作用于跨度，父标识不因筛选改写。任一筛选非法都在读取聚合前抛 `ValueError`——不调用 clock、不产生部分结果；没有匹配项时对应数组为空。命中的样本统计按原规则重算，排序顺序不变，返回的字典、数组与记录均为独立副本；同一组筛选可重复使用得到相同结果，筛选不写入、清空或重排内部数据。`json()` 同条件下可 JSON 表示的字段与 `snapshot()` 一致，`error` 继续按既有规则转换。
- `Telemetry.from_snapshot(payload, clock=time.time)`：离线把 `snapshot()` 字典或 `json()` 文本重建为独立实例，计数器、样本原值与跨度的 parent/start/end/error 全部带回，样本统计按公开浮点规则从 values 重算（输入中存在的统计字段须与重算一致，空 values 不得携带统计）。payload 顶层只能含 `counters`、`samples`、`spans` 三个数组；缺字段、多字段、非法 JSON、重复记录、无效标签、不可哈希跨度标识、不可严格 JSON 表示的值等一律抛 `ValueError`，且失败前不留下半成品实例、不改动已有实例。恢复过程不联网、不读写文件、不修改输入；恢复后与原对象互不共享数据，`open` 跨度可继续用 `finish()` 结束。
- `merge_snapshot(payload)`：把一份离线分片快照原子合并进当前实例，返回 `None`；输入接受范围与 `from_snapshot` 相同（字典、JSON 文本、UTF-8 字节），校验规则也一致。计数器按服务、名称、归一化标签定位，相同键按加法语义累加，不可相加抛 `ValueError`；相同键样本先保留当前 `values` 再按输入顺序追加输入值，统计按公开浮点规则从完整 values 重算；跨度以 service 与 span 联合定位，仅一方出现时整体复制，两方完全一致时幂等，不一致时抛 `ValueError`。先完整解析校验、再一次性提交：任何恢复或合并错误都使当前实例的聚合、跨度与时钟配置保持不变，输入不被改写，合并后数据不与 payload 共享可变对象；不联网、不读写文件。
- `Telemetry.diff_snapshots(before, after)`：面向离线诊断的只读快照差异查询，返回全新的、可 JSON 序列化的差异字典。两个输入都接受 `snapshot()` 字典、`json()` 文本或 UTF-8 字节，并按恢复入口相同的严格 JSON、字段、重复记录、标签与可哈希标识规则解析；任一输入无效统一抛 `ValueError`，抛出前不返回部分结果。方法不读取 clock、不联网、不修改任何输入对象。结果顶层只有 `counters`、`samples`、`spans`，每一项都只含 `added`、`removed`、`changed` 三个数组；counter/sample 记录以 service、name、labels 的完整组合定位，span 记录以 service、span 定位——只出现在 after 的完整记录进入 added，只出现在 before 的进入 removed，同一定位但内容不同的进入 changed，changed 元素只含 `before` 与 `after` 两份相互独立的完整记录。样本先按 `values` 与公开浮点统计重新归一再比较，输入携带或省略等价统计字段不算变化；跨度 open→closed、error 变化、parent 变化等任意字段差异都算 changed。added/removed 沿用对应快照的稳定排序，changed 按 after 记录的快照排序，无差异对应数组为空。返回的记录与嵌套标签均可安全修改，与两个输入互不共享；该入口不向快照写入字段，也不改变异常状态判定。
- `batch(events)`：离线诊断数据的批量回放入口，按输入顺序一次提交，成功返回 `None`。`events` 只能是事件对象（dict）组成的列表或元组；每个事件以 `op` 指定 `inc`、`observe`、`start` 或 `finish`，其余字段沿用对应公开入口的名称（`name`/`value`/`labels`/`span`/`parent`/`error`/`service`）、默认值（`value=1`、`labels=()`、`parent=None`、`error=None`、`service=None`）与校验规则；批次内后续事件可以使用前面事件刚建立的跨度。空批次视为成功且不改变状态。成功提交后 `snapshot`/`json`/`query`/`trace` 的结果与按同一顺序直接调用对应入口完全一致，每个 `start`/`finish` 仍只读取一次 clock 且读取次序相同。事件不是对象、不是列表/元组、`op` 缺失或未知、字段不属于所选操作、缺失必需字段（`name`/`span`、observe 的 `value`），或事件值违反服务、标签、样本、跨度规则（含重复开始、结束不存在或已结束的跨度），统一抛 `ValueError`。提交具有原子可见性：任何事件被拒绝时计数器、样本、跨度、查询结果与后续快照保持调用前状态，且拒绝判定阶段不读取 clock；提交阶段 clock 自身抛出的异常原样向调用方传播并同样恢复调用前状态。批量入口不修改传入事件对象或其中的标签和值，也不新增快照字段。

## percentile 示例

```python
t = Telemetry()
t.observe("latency_ms", 42.0)
t.percentile("latency_ms", 50)            # 42.0：单值序列任意分位数都是该值

for v in (10, 20, 30, 40):
    t.observe("size", v, service="api")
t.percentile("size", 50, service="api")   # 25.0：偶数个样本取中间两项的插值
t.percentile("size", 0, service="api")    # 10.0：q=0 即最小值
t.percentile("size", 100, service="api")  # 40.0：q=100 即最大值

t.percentile("missing", 95)               # None：样本键不存在
t.percentile("size", 95)                  # None：服务不同即键不存在
t.percentile("size", 101, service="api")  # ValueError：q 超出 [0, 100]
t.percentile("size", True, service="api") # ValueError：bool 不是可接受的 q
```

