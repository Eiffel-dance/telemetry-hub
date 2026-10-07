# Telemetry Hub

A dependency-free Python reference implementation for observability, metrics, tracing.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个轻量级本地遥测聚合器，统一记录计数器、数值样本和带父子关系的跨度，并输出稳定排序的 JSON 快照。聚合必须区分服务和标签，浮点统计采用公开规则，未结束跨度和异常状态要可查询；整个组件不发送网络请求，快照可直接用于离线诊断和回放。

## API

- `inc(name, value=1, labels=(), service=None)` / `observe(name, value, labels=(), service=None)`：可选 `service`，缺省归一化为空字符串（默认服务），显式服务名必须是非空字符串；同名同标签但服务不同分别聚合。标签按键字典序归一化，重复键或不可 JSON 序列化的标签抛 `ValueError`，且不改变已有聚合。
- 可选序列容量保护：创建实例时可传 `max_series`（`Telemetry(clock=time.time, max_series=None)`，`from_snapshot`/`restore` 同样接受该关键字），省略或显式 `None` 表示不设上限；其他值必须是非 `bool` 的非负整数（`0` 合法，`-1`、`1.0`、`True`、`"1"` 等一律抛 `ValueError`）。指标序列按计数器或样本的类型、`service`、`name` 与归一化后的完整 `labels` 组合唯一定位：同名同标签的计数器与样本分别计数，跨度不占用配额。`inc`/`observe` 更新已有序列时照常执行；首次写入新序列若会使序列总数超过 `max_series`，抛出 `TelemetryCapacityError`（`ValueError` 的子类），拒绝在修改聚合或读取 clock 前完成，原有数据、输入对象及后续快照保持不变。
- `capacity()`：新增的只读容量入口，返回独立字典，固定含 `limit`、`used`、`remaining`：`limit` 为创建时的 `max_series`（无限制时为 `None`），`used` 为当前计数器与样本序列总数，`remaining` 为剩余配额（`limit - used`，无限制时为 `None`）。该入口不读 clock、不写快照或 JSON，重复调用结果一致，修改返回字典不影响实例；容量信息不进入 `snapshot()`/`json()`，也不影响 digest。
- `TelemetryCapacityError`：序列容量超限时抛出的公开异常类型，`ValueError` 的子类，既有按 `ValueError` 捕获的调用方行为不变。容量参数本身非法时仍抛普通 `ValueError`。`batch`、`restore_snapshot`、`merge_snapshot` 和 `merge_snapshots` 按同一规则检查容量并保持现有原子性：任一新序列超限，整体抛 `TelemetryCapacityError`，聚合、跨度、时钟配置和输入快照保持调用前状态，校验阶段不读 clock；快照格式错误仍抛 `SnapshotFormatError`（格式校验先于容量检查）。实例级恢复和合并沿用当前 `max_series`（`restore_snapshot`/`merge_snapshot` 不接受容量参数）；带容量创建或恢复的实例继续写入的结果与无容量全量记录完全一致。
- 样本保留按写入顺序的原值 `values`；快照对非空样本附 `count`、`sum`（按写入顺序累加）、`minimum`、`maximum`、`mean`（`sum/count`，不四舍五入）。NaN/无穷值抛 `ValueError`，空样本不产生统计。
- `start(span, parent=None, service=None, labels=())` / `finish(span, error=None, service=None)`：service 与跨度标识共同定位跨度；父标识原样保留。`start` 可携带可选 `labels`，标签沿用 `observe` 的成对输入、键排序、重复键与严格 JSON 校验，归一化只构造新结构、不改写调用方对象；标签在跨度开始时确定并随记录保存，`finish` 只结束已有的 service/span 跨度，不能改写标签。`start` 写入前先完成 service、span 与 labels 校验（service 缺省归一化为空字符串，显式传入必须是非空字符串；span 必须可哈希），相同 service/span 标识已存在跨度时——无论仍未结束还是已经结束——都抛 `ValueError`，不覆盖原有 parent/start/end/error/labels，也不推进 clock；首次创建成功时以一次 clock 结果作为 `start`，返回 `None`。`finish` 只结束当前存在且 `end` 仍为空的跨度：标识不存在或跨度已结束时抛 `ValueError`，不改变任何聚合数据、不生成新的时间戳；有效调用把 `end` 设为一次 clock 结果、把 `error` 设为传入值并返回 `None`。start/finish 收到不可哈希 span 一律抛 `ValueError`，所有被拒绝的调用都不留下半条记录，也不影响其他服务的数据。
- `query(status, service=None, labels=None)`：仅接受 `'open'`（`end` 为空）、`'closed'`（`end` 已写入，成功结束与带异常结束都算）和 `'error'`（已结束且 `error` 非空），其他字符串、空值或非字符串值抛 `ValueError`（拒绝时不调用 clock、不留部分结果），无匹配返回 `[]`；`service` 为可选筛选，语义与 `snapshot()`/`json()` 的服务筛选一致：省略（或显式 `None`）即不按服务限制，结果与只传 `status` 时逐项一致，提供时只能是字符串，空字符串表示默认服务，数字、字节串、列表等其他类型抛 `ValueError`；`labels` 为可选标签筛选，省略（或显式 `None`）匹配全部标签，提供时沿用 `observe` 的成对输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合精确匹配（显式空序列只命中无标签记录），非法标签抛 `ValueError`，任何被拒绝的调用都不读取 clock、不改变聚合状态；结果含 `span/service/parent/start/end/error`，有标签的跨度附加可安全修改的 `labels` 字段，按服务、开始时间、标识排序；返回值为独立列表与独立记录，调用方修改不影响聚合器，`error` 原值（含异常对象）保留。
- `trace(span, service=None)`：离线诊断用跨度树查询。service 归一化规则与 `start`/`finish` 一致，跨度标识不可哈希抛 `ValueError`；根跨度不存在返回 `None`，存在时返回独立树对象，节点含 `span/service/parent/start/end/error` 与 `children` 数组，有标签的节点附加可安全修改的 `labels` 字段，`children` 递归包含 parent 与当前节点标识精确相等且 service 相同的直接子跨度，每层按服务、开始时间、标识排序，无子节点为空数组。父标识指向其他服务或不存在的跨度按无子节点处理；从根可达的父子引用构成环时抛 `ValueError`，不返回部分树。返回对象及其列表与聚合器互不共享，查询不改动任何已有数据。
- `percentile(name, q, labels=(), service=None)`：只读的分位数查询，用于离线诊断样本分布，调用方不需要先导出或修改聚合器状态。`service` 与 `labels` 的归一化规则与 `observe` 完全一致（`service` 缺省归一化为空字符串，显式传入必须是非空字符串；标签按键字典序归一化，重复键或不可 JSON 序列化抛 `ValueError`），`name` 必须可哈希才能作为样本键，不可哈希抛 `ValueError`。`q` 只接受非 `bool` 的 `int`/`float`，必须为有限值且落在 `[0, 100]` 闭区间，否则统一抛 `ValueError`；任何拒绝都发生在读取样本之前——不读 clock、不改变计数器、样本、跨度或输入对象，也不产生部分结果。样本键不存在或序列为空返回 `None`；命中时按公开浮点规则把每个值转换为数值并升序排序（排序副本不回写 `values`），以位置 `(n-1)*q/100` 线性插值，位置为整数时直接取该项，`q=0`/`q=100` 分别得到最小值/最大值，返回 Python `float`。该入口只读取当前聚合，重复调用结果相同，也不新增 `snapshot()`/`json()` 字段。
- `histogram(name, boundaries, labels=(), service=None)`：只读的数值分布计数，用于离线诊断，调用方不需要先导出或修改聚合器状态。`service`、`labels` 与 `name` 的定位、默认服务、标签规范化与精确匹配规则与 `observe`/`percentile` 完全一致，非法值统一抛 `ValueError`。`boundaries` 必须是非空 `list` 或 `tuple`，元素必须是非 `bool` 的 `int`/`float`、有限且严格递增，任一不合法统一抛 `ValueError`，且全部边界校验在读取样本之前完成（不读 clock、不产生部分结果）。样本键不存在或序列为空返回 `None`；命中时每个样本先按 `observe` 的规则转成有限 `float`（已有数据无法转换时同样抛 `ValueError`，不返回部分结果），再按左开右闭分桶：第一桶统计小于等于 `boundaries[0]`，中间桶统计大于前一边界且不超过当前边界，最后一桶统计大于最后边界。返回全新字典，只含 `boundaries`（与输入顺序一致的独立列表）、`counts`（长度为 `len(boundaries)+1` 的整数列表）与 `count`（各桶计数之和）。每次调用都重新构造结果，修改返回值不影响内部状态，不读 clock、不改变样本顺序、也不触发序列化；该入口不改变 `percentile`/`snapshot()`/`json()` 的字段与结果。
- `sample_summary(name, labels=None, service=None)`：只读的同名样本序列汇总，用于离线诊断时一次合并同名序列，调用方不需要先导出快照再自行合并，全程不联网。`name` 必须可哈希，不可哈希统一抛 `ValueError`。`service` 省略或显式 `None` 匹配所有服务；提供时只能是字符串，按快照筛选语义精确匹配，空字符串表示默认服务，其他类型统一抛 `ValueError`。`labels` 省略或显式 `None` 匹配所有完整标签集合；提供时沿用 `observe` 的成对输入、键排序、重复键拒绝与严格 JSON 校验，按归一化后的完整标签集合精确匹配（显式空序列只匹配无标签样本）。全部参数校验先于任何样本读取：不读 clock、不产生部分结果、不修改聚合状态与输入标签。序列选取与排序与快照完全一致：先按快照对服务、名称、标签的现有稳定顺序排列全部样本序列，再选出同名、服务与标签匹配且 `values` 非空的序列（空 `values` 序列不参与汇总）；每条序列内部沿用 `values` 的写入顺序，把所有值按公开有限浮点规则转换后合并——任一历史值无法转换为有限数值（含超大整数溢出、自定义 `__float__` 异常、NaN/无穷）或转换过程出现异常，统一抛 `ValueError`，且全部值先完成转换才形成结果，绝不返回部分字典。成功返回全新字典，只含 `series_count`、`count`、`sum`、`minimum`、`maximum`、`mean`：`series_count` 是参与汇总的非空序列数，`count` 是实际值总数，`sum` 自 `0.0` 起按上述确定顺序累加，`minimum`/`maximum` 为全体数值的最小/最大值，`mean` 等于 `sum` 除以 `count`；两个计数字段为 Python `int`，其余统计字段为 Python `float`。没有匹配序列或没有可保留值时返回 `None`。返回结果可安全修改，重复调用结果一致；该查询不写入 `snapshot`/`json`/`digest` 或恢复载荷，也不新增快照字段，现有容量限制、跨度生命周期、异常保存及恢复、合并与批量回放行为保持原状。
- `histogram_summary(name, boundaries, labels=None, service=None)`：只读的同名样本序列合并分桶查询，用于离线诊断时把满足筛选的多条同名序列合并为一次离线分桶，调用方不需要先导出快照再自行合并，全程不联网。`name` 必须可哈希，不可哈希统一抛 `ValueError`。`service` 省略或显式 `None` 匹配所有服务；提供时沿用快照筛选语义，只能是字符串，空字符串表示默认服务，其他类型统一抛 `ValueError`。`labels` 省略或显式 `None` 匹配所有完整标签集合；提供时沿用 `observe` 的成对输入、键排序、重复键拒绝与严格 JSON 校验，按归一化后的完整标签集合精确匹配（显式空标签只命中无标签样本）。`boundaries` 必须是非空 `list`/`tuple`，元素必须是非 `bool` 的 `int`/`float`、有限且严格递增，任一不合法统一抛 `ValueError`。`name`、`service`、`labels` 与 `boundaries` 的全部校验先于任何样本读取：不读 clock、不产生部分结果、不改变聚合、不修改输入标签。序列选取与排序和快照完全一致：先按快照对服务、名称、标签的现有稳定顺序排列全部样本序列，再选出同名、服务与标签匹配且 `values` 非空的序列（空 `values` 序列不参与分桶）；每条序列内部沿用 `values` 的写入顺序拼接，把所有值按公开有限浮点规则转换——任一历史值无法转换为有限数值（含超大整数溢出、NaN、无穷与自定义 `__float__` 异常）统一抛 `ValueError`，且全部值先完成转换才分桶，绝不返回部分字典。分桶为左开右闭：第一桶统计小于等于 `boundaries[0]`，中间桶统计大于前一边界且不超过当前边界，最后一桶统计大于最后边界。成功返回全新字典，只含 `boundaries`（与输入顺序一致的独立列表副本）、`counts`（长度为 `len(boundaries)+1` 的整数列表）与 `count`（各桶之和）；没有可参与序列时返回 `None`。返回结果可安全修改，重复调用结果一致；该查询不写入 `snapshot`/`json`/`digest` 或恢复载荷，也不新增快照字段，恢复或合并得到的样本同样可查询，现有容量限制、跨度生命周期、异常保存及恢复、合并与批量回放行为保持原状。
- `span_duration_stats(service=None, labels=None, status="closed")`：只读的跨度耗时汇总，用于离线诊断直接读取已记录跨度的耗时，调用方不需要先导出快照，全程不联网。只统计已经结束的跨度：`status` 只接受 `'closed'` 与 `'error'`，`'closed'` 包含所有 `end` 已写入的跨度（成功结束与带异常结束都算），`'error'` 只包含其中 `error` 不为 `None` 的跨度（`0`、`False`、空容器等假值也不例外）；传入 `'open'` 或任何其他值（含 `None` 与非字符串）统一抛 `ValueError`。`service` 与 `labels` 的筛选语义与 `query` 完全一致：`service` 缺省（`None`）匹配全部服务，提供时只能是字符串，空字符串表示默认服务，其他类型抛 `ValueError`；`labels` 缺省匹配全部标签，提供时沿用 `observe` 的成对输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合精确匹配（显式空标签只命中无标签跨度）。服务、标签或状态校验失败都在读取任何跨度之前抛 `ValueError`——不读 clock、不产生部分结果、不改变聚合状态。筛选后没有已结束跨度时返回 `None`；命中时返回全新字典，含 `values`、`count`、`sum`、`minimum`、`maximum`、`mean`：`values` 按 `query`/`snapshot` 对跨度使用的稳定顺序（服务、开始时间、标识）排列，每个元素是对应跨度 `end` 与 `start` 各自按现有样本统计的有限浮点规则转换后相减得到的 Python `float`，任一结束跨度的时间戳无法转换为有限数值（含超大整数溢出、自定义 `__float__` 异常、NaN/无穷）统一抛 `ValueError` 且不返回部分结果（未结束跨度不参与统计，其时间戳不会被转换）；`count` 等于 `values` 长度，`sum` 自 `0.0` 起按 `values` 顺序累加，`minimum`/`maximum` 为这批耗时的最小/最大值，`mean` 等于 `sum` 除以 `count`。每次调用都重新构造结果字典与 `values` 列表，可安全修改，不与内部状态共享；该入口纯只读，不读 clock、不改变样本与跨度顺序，也不向 `snapshot()`/`json()` 新增字段，未结束跨度仍可通过原入口查询，异常对象的保存与 JSON 占位规则不变。
- `spans_by_duration(minimum=None, maximum=None, service=None, labels=None, status="closed")`：只读的已结束跨度耗时区间查找，用于离线诊断直接按耗时挑出跨度，调用方不需要先导出快照，全程不联网，也不写入快照或任何内部状态。`status` 只接受 `'closed'` 与 `'error'`：`'closed'` 包含所有 `end` 已写入的跨度（成功结束与带异常结束都算），`'error'` 只包含其中 `error` 不为 `None` 的跨度（`0`、`False`、空容器等假值也不例外）；传入 `'open'` 或任何其他值（含 `None` 与非字符串）统一抛 `ValueError`。`service` 与 `labels` 的筛选语义与 `query` 完全一致：`service` 缺省（`None`）匹配全部服务，提供时只能是字符串，空字符串表示默认服务，其他类型抛 `ValueError`；`labels` 缺省匹配全部标签，提供时沿用 `observe` 的成对输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合精确匹配（显式空标签只命中无标签跨度）。`minimum`/`maximum` 省略（`None`）表示该侧无界；提供时只能是非 `bool` 的 `int`/`float` 且必须有限（`NaN`、无穷、字符串、字节串、`Decimal`、`bool` 等一律抛 `ValueError`），同时给出时 `minimum` 不得大于 `maximum`。全部条件（status、service、labels、两侧边界与区间关系）先校验通过才读取任何跨度：任一失败统一抛 `ValueError`，不读 clock、不产生部分结果、不改变聚合状态与调用方对象。候选是 `closed`/`error` 筛选后的已结束跨度（未结束跨度不进入候选、不转换也不返回），按 `query`/`snapshot` 的稳定顺序（服务、开始时间、标识）把每个候选的 `end` 与 `start` 各自按现有样本统计的有限浮点规则转换后相减（`float(end) - float(start)`）：转换失败、`NaN`、无穷、超大整数溢出或自定义 `__float__` 抛异常都统一抛 `ValueError`，且全部候选先转换完再按区间筛选，不返回部分结果。区间为闭区间 `minimum <= duration <= maximum`，缺省侧视为无界。成功返回全新列表，无匹配返回空列表；每项是 `query` 同形的独立记录（`span`、`service`、`parent`、`start`、`end`、`error`，有标签时附加可安全修改的 `labels`），再增加 `duration` 字段，值为 Python `float`；修改返回列表、记录或标签不影响聚合器，`error` 原值（含异常对象）按 `query` 保留。重复调用结果相同；该入口纯只读、不联网，`duration` 不会写入 `snapshot()`/`json()`，其他恢复、合并、批量、摘要与 JSON 行为保持原状。
- `spans_by_start_time(minimum=None, maximum=None, service=None, labels=None, status=None)`：只读的跨度绝对开始时间区间查找，用于回放时直接按开始时间定位某个时间段内启动的请求，调用方不需要先导出快照，全程不联网，也不写入快照或任何内部状态。与 `spans_by_duration` 比较耗时不同，本入口直接比较跨度的 `start`，且 `status` 缺省（`None`）保留全部跨度（含未结束跨度），提供时只接受 `'open'`/`'closed'`/`'error'` 并使用 `query` 的同一状态定义（`'open'` 为 `end` 为空；`'closed'` 为 `end` 已写入，成功结束与带异常结束都算；`'error'` 为已结束且 `error` 不为 `None`，`0`、`False`、空容器等假值也不例外），其他值（含非字符串）统一抛 `ValueError`。`service` 与 `labels` 的筛选语义与 `query` 完全一致：`service` 缺省（`None`）匹配全部服务，提供时只能是字符串，空字符串表示默认服务，其他类型抛 `ValueError`；`labels` 缺省匹配全部标签，提供时沿用 `observe` 的成对输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合精确匹配（显式空标签只命中无标签跨度）。`minimum`/`maximum` 省略（`None`）表示该侧无界；提供时只能是非 `bool` 的 `int`/`float` 且必须有限（`NaN`、无穷、字符串、字节串、`Decimal`、`bool` 等一律抛 `ValueError`），同时给出时 `minimum` 不得大于 `maximum`。全部条件（status、service、labels、两侧边界与区间关系）先校验通过才读取任何跨度：任一失败统一抛 `ValueError`，不读 clock、不产生部分结果、不改变聚合状态与调用方对象。候选是筛选后的跨度（`status` 缺省时包含未结束跨度），按 `query`/`snapshot` 的稳定顺序（服务、开始时间、标识）把每个候选的 `start` 单独按现有样本统计的有限浮点规则转换：转换失败、`NaN`、无穷、超大整数溢出或自定义 `__float__` 抛异常都统一抛 `ValueError`，未结束跨度只校验 `start`（不接触其为空的 `end`），且全部候选先转换完再按区间筛选（即使异常跨度落在请求区间之外也照样先转换），不返回部分结果。区间为闭区间 `minimum <= start <= maximum`，缺省侧视为无界。成功返回全新列表，无匹配返回空列表；每项是 `query` 同形的独立记录（`span`、`service`、`parent`、`start`、`end`、`error`，有标签时附加可安全修改的 `labels`），不追加任何派生字段；修改返回列表、记录或标签不影响聚合器，`error` 原值（含异常对象）按 `query` 保留。重复调用结果相同；该入口纯只读、不联网，筛选与派生值不会写入 `snapshot()`/`json()`/`digest`，恢复或合并得到的跨度同样可查询，其他写入、查询、恢复、合并、批量、摘要与 JSON 行为保持原状。
- `snapshot(service=None, labels=None, status=None)` / `json(service=None, labels=None, status=None)`：计数器和样本按服务、名称、标签排序，跨度按服务、开始时间、标识排序；JSON 紧凑（`separators=(",", ":")`）且键序稳定（`sort_keys=True`）。三个筛选全部可省略，省略任一筛选即不按该维度限制，无参数调用结果与既有行为逐项一致，仍返回 `counters`、`samples`、`spans` 三个数组。`service` 缺省匹配全部服务，提供时只能是字符串，空字符串表示默认服务，其他类型抛 `ValueError`；`labels` 缺省匹配全部标签，提供时沿用 `observe` 的成对标签输入、键排序、重复键与严格 JSON 校验，按归一化后的完整标签集合与计数器/样本/跨度精确匹配（显式空序列只命中无标签记录），标签值无法通过既有规则时统一抛 `ValueError`；`status` 缺省保留所有服务的跨度，提供时只能是 `open`/`closed`/`error` 并使用 `query` 对结束与异常的既有定义。`service` 与 `labels` 对三个数组同时生效，`status` 只作用于跨度，父标识不因筛选改写。有标签的跨度条目附加 `labels` 字段（按键序的成对列表），无标签条目保持既有字段形状。任一筛选非法都在读取聚合前抛 `ValueError`——不调用 clock、不产生部分结果；没有匹配项时对应数组为空。命中的样本统计按原规则重算，排序顺序不变，返回的字典、数组与记录均为独立副本；同一组筛选可重复使用得到相同结果，筛选不写入、清空或重排内部数据。`json()` 同条件下可 JSON 表示的字段与 `snapshot()` 一致，`error` 继续按既有规则转换。
- `Telemetry.from_snapshot(payload, clock=time.time, max_series=None)`：离线把 `snapshot()` 字典或 `json()` 文本重建为独立实例，计数器、样本原值与跨度的 parent/start/end/error/labels 全部带回，样本统计按公开浮点规则从 values 重算（输入中存在的统计字段须与重算一致，空 values 不得携带统计）。`max_series` 为重建实例的可选序列容量上限（省略或 `None` 不设限，非法值抛 `ValueError`），快照内计数器与样本序列总数超限时抛 `TelemetryCapacityError`，跨度不占配额；格式校验先于容量检查。payload 顶层只能含 `counters`、`samples`、`spans` 三个数组；跨度条目可携带可选的 `labels` 字段（成对标签数组，校验与 `observe` 一致），缺少 `labels` 的旧快照按空标签恢复；缺字段、多字段、非法 JSON、重复记录、无效标签、不可哈希跨度标识、不可严格 JSON 表示的值等一律抛 `SnapshotFormatError`（`ValueError` 的子类），且失败前不留下半成品实例、不改动已有实例。恢复过程不联网、不读写文件、不修改输入；恢复后与原对象互不共享数据，`open` 跨度可继续用 `finish()` 结束。
- `SnapshotFormatError`：快照恢复格式错误的公开异常类型，`ValueError` 的子类。所有快照恢复入口（`from_snapshot`/`merge_snapshot`/`diff_snapshots`/`restore`/`restore_snapshot`）对不可解析的 JSON、非对象顶层、不受支持的版本、缺失或类型错误的字段、非有限数值、无法按既有规则规范化的标签、悬空或成环的父子引用、重复跨度标识等输入问题统一抛出本异常；既有按 `ValueError` 捕获的调用方行为不变。
- `Telemetry.restore(payload, clock=time.time, max_series=None)` / `restore_snapshot(payload)`：快照恢复与离线续采的严格入口——一次诊断结果在内存中还原后可继续接收事件。`restore` 是类方法，返回可继续记录的新实例（可通过 `max_series` 为新实例指定序列容量上限，规则与构造函数一致）；`restore_snapshot` 是实例方法，把快照原子恢复到当前实例（替换全部聚合状态），沿用实例当前的 `max_series`，成功返回 `None`，任何失败（含序列超限的 `TelemetryCapacityError`）都使当前实例的聚合、跨度、时钟配置与容量上限保持调用前状态。输入接受 `snapshot()` 字典、`json()` 文本或 UTF-8 字节；顶层在 `counters`、`samples`、`spans` 之外可携带可选的 `version`：`version` 只接受非 `bool` 整数且必须落在公开兼容版本集合内（当前为 `{1}`，未携带 `version` 的既有快照按版本 1 的既定规则读取），其他版本一律抛 `SnapshotFormatError`。校验在 `from_snapshot` 的既有规则之上增加父子引用检查：每个非空 `parent` 必须指向同一服务内已存在的跨度标识（与 `trace` 对父子关系的既定定义一致，指向其他服务或不存在的跨度都视为悬空引用），且父子关系不得成环。恢复后的聚合器保留服务与标签维度、计数值、样本数量及公开浮点统计结果、每个跨度的开始/结束状态、异常信息、标签与父跨度关系；继续用 `inc`/`observe`/`start`/`finish` 记录时沿用既有累加、更新与生命周期语义（`finish` 只结束指定跨度，不隐式结束其他跨度），随后生成的快照与从同一事件序列全量记录得到的快照字段值与稳定排序一致，浮点结果不因恢复改变。同一快照恢复到两个新实例再按相同顺序写入相同事件，查询结果与 JSON 快照逐字一致；空快照得到可继续使用的空聚合器。恢复全程不联网、不读写文件、不修改输入，恢复后的数据与 payload 互不共享可变对象。
- `merge_snapshot(payload)`：把一份离线分片快照原子合并进当前实例，返回 `None`；输入接受范围与 `from_snapshot` 相同（字典、JSON 文本、UTF-8 字节），校验规则也一致。计数器按服务、名称、归一化标签定位，相同键按加法语义累加，不可相加抛 `ValueError`；相同键样本先保留当前 `values` 再按输入顺序追加输入值，统计按公开浮点规则从完整 values 重算；跨度以 service 与 span 联合定位，仅一方出现时整体复制，两方完全一致（含标签）时幂等，不一致时（含同一 service/span 的标签冲突）抛 `ValueError`。先完整解析校验、再一次性提交：任何恢复或合并错误都使当前实例的聚合、跨度与时钟配置保持不变，输入不被改写，合并后数据不与 payload 共享可变对象；不联网、不读写文件。
- `Telemetry.diff_snapshots(before, after, service=None, labels=None, status=None)`：面向离线诊断的只读快照差异查询，返回全新的、可 JSON 序列化的差异字典。两个输入都接受 `snapshot()` 字典、`json()` 文本或 UTF-8 字节，并按恢复入口相同的严格 JSON、字段、重复记录、标签与可哈希标识规则解析；任一输入无效统一抛 `SnapshotFormatError`，抛出前不返回部分结果。方法不读取 clock、不联网、不修改任何输入对象。三个筛选全部可省略，省略时与两参数调用逐项一致；`service`/`labels`/`status` 的取值与校验语义和 `snapshot()` 完全相同（`service` 缺省匹配全部、提供时只能是字符串且空串表示默认服务；`labels` 缺省匹配全部、提供时按规范化后的完整标签集合精确匹配计数器、样本与跨度；`status` 缺省匹配全部跨度、提供时只接受 `open`/`closed`/`error` 且只作用于跨度）。非法筛选在解析输入前统一抛 `ValueError`。before 与 after 各自先完成完整快照解析与严格校验，再独立应用同一组筛选，最后按既有定位键生成差异；筛选导致某条记录只在一侧可见时，按筛选后的视图判定 added 或 removed。结果顶层只有 `counters`、`samples`、`spans`，每一项都只含 `added`、`removed`、`changed` 三个数组；counter/sample 记录以 service、name、labels 的完整组合定位，span 记录以 service、span 定位——只出现在 after 的完整记录进入 added，只出现在 before 的进入 removed，同一定位但内容不同的进入 changed，changed 元素只含 `before` 与 `after` 两份相互独立的完整记录。样本先按 `values` 与公开浮点统计重新归一再比较，输入携带或省略等价统计字段不算变化；跨度 open→closed、error 变化、parent 变化、labels 变化等任意字段差异都算 changed。added/removed 沿用对应快照的稳定排序，changed 按 after 记录的快照排序，无差异对应数组为空。返回的记录与嵌套标签均可安全修改，与两个输入互不共享；该入口不向快照写入字段，也不改变异常状态判定。
- `batch(events)`：离线诊断数据的批量回放入口，按输入顺序一次提交，成功返回 `None`。`events` 只能是事件对象（dict）组成的列表或元组；每个事件以 `op` 指定 `inc`、`observe`、`start` 或 `finish`，其余字段沿用对应公开入口的名称（`name`/`value`/`labels`/`span`/`parent`/`error`/`service`，其中 `start` 事件可携带 `labels`）、默认值（`value=1`、`labels=()`、`parent=None`、`error=None`、`service=None`）与校验规则；批次内后续事件可以使用前面事件刚建立的跨度。空批次视为成功且不改变状态。成功提交后 `snapshot`/`json`/`query`/`trace` 的结果与按同一顺序直接调用对应入口完全一致，每个 `start`/`finish` 仍只读取一次 clock 且读取次序相同。事件不是对象、不是列表/元组、`op` 缺失或未知、字段不属于所选操作、缺失必需字段（`name`/`span`、observe 的 `value`），或事件值违反服务、标签、样本、跨度规则（含重复开始、结束不存在或已结束的跨度），统一抛 `ValueError`。提交具有原子可见性：任何事件被拒绝时计数器、样本、跨度、查询结果与后续快照保持调用前状态，且拒绝判定阶段不读取 clock；提交阶段 clock 自身抛出的异常原样向调用方传播并同样恢复调用前状态。批量入口不修改传入事件对象或其中的标签和值，也不新增快照字段。
- `digest(service=None, labels=None, status=None)`：快照完整性指纹，用于离线诊断确认两份结果是否来自同一聚合状态。视图生成规则与 `snapshot()`/`json()` 完全一致——同一组 service/labels/status 筛选、同样的稳定排序（计数器和样本按服务、名称、标签，跨度按服务、开始时间、标识）、样本统计按原规则重算、不可严格 JSON 表示的 `error` 按 `json()` 既有规则替换为只含 type/message 的占位对象；指纹就是对该视图紧凑 JSON 文本（`sort_keys=True`、`separators=(",", ":")`）的 UTF-8 字节计算 SHA-256，返回固定 64 个字符的小写十六进制字符串。相同聚合状态与同一组筛选重复调用必然得到相同指纹；计数值、样本原值、跨度父子关系、标签、结束状态或异常内容变化后，受影响视图的指纹改变。筛选只决定哪些数组条目进入摘要，绝不写入、清空或重排聚合器，也不向 `snapshot()`/`json()` 新增字段（digest 不会出现在任何快照中）。非法筛选与 `json()` 一样在读取聚合前抛 `ValueError`；整个过程纯只读：不读取 clock、不联网、不写文件、不改写任何输入；紧凑 JSON 无法按既有 json 规则完成时统一抛 `ValueError`。
- `Telemetry.verify_digest(payload, expected, service=None, labels=None, status=None)`：回放前校验外部快照，返回严格的 `True`/`False`。`payload` 的接受范围与 `restore` 完全一致（`snapshot()` 字典、`json()` 文本或 UTF-8 字节），遵循同一版本兼容范围：先按既有快照格式、标签、重复记录、统计一致性与跨度父子引用规则完成解析（无法恢复时抛 `SnapshotFormatError`），再以规范化后的当前视图（同一组筛选、排序、统计重算与异常占位规则）计算指纹。因此字典键顺序、标签输入顺序和可由 values 重算的样本统计字段不会造成误判。`expected` 只能是恰好 64 个字符的小写十六进制字符串，格式不合法抛 `ValueError`；非法筛选抛 `ValueError`，且 expected 格式与筛选校验先于 payload 解析。校验全部通过后摘要匹配返回 `True`，格式正确但摘要不同返回 `False`。全程不读取 clock、不联网、不写文件、不改写输入，紧凑 JSON 无法按既有 json 规则生成时统一抛 `ValueError`。

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

## histogram 示例

```python
t = Telemetry()
for v in (0, 1, 2, 2, 5, 9, 10, 10.5, 11):
    t.observe("x", v)
t.histogram("x", [1, 5, 10])
# {'boundaries': [1, 5, 10], 'counts': [2, 3, 2, 2], 'count': 9}
# <=1: 0,1 ｜ (1,5]: 2,2,5 ｜ (5,10]: 9,10 ｜ >10: 10.5,11（左开右闭）

t.histogram("missing", [1, 2])            # None：样本键不存在
t.histogram("x", [])                      # ValueError：boundaries 必须非空
t.histogram("x", [5, 1])                  # ValueError：必须严格递增
t.histogram("x", [1, True])               # ValueError：bool 不是合法边界
```

## sample_summary 示例

```python
t = Telemetry()
t.observe("lat", 1); t.observe("lat", 2)                 # 默认服务、无标签
t.observe("lat", 4, labels=(("k", "v"),))               # 默认服务、带标签
t.observe("lat", 8, service="api"); t.observe("lat", 16, service="api")

t.sample_summary("lat")
# {'series_count': 3, 'count': 5, 'sum': 31.0,
#  'minimum': 1.0, 'maximum': 16.0, 'mean': 6.2}
# 序列按快照顺序（服务、名称、标签）取，序列内按写入顺序

t.sample_summary("lat", service="")        # 只汇总默认服务（含带标签序列）
# {'series_count': 2, 'count': 3, 'sum': 7.0, ...}
t.sample_summary("lat", labels=(), service="api")
# {'series_count': 1, 'count': 2, 'sum': 24.0,
#  'minimum': 8.0, 'maximum': 16.0, 'mean': 12.0}
t.sample_summary("lat", labels=(("k", "v"),))
# {'series_count': 1, 'count': 1, 'sum': 4.0, ...}：显式空序列才匹配无标签
t.sample_summary("missing")                # None：没有同名序列
t.sample_summary("lat", service=1)         # ValueError：service 必须是字符串
t.sample_summary(["lat"])                  # ValueError：name 必须可哈希
```

## histogram_summary 示例

```python
t = Telemetry()
t.observe("lat", 1); t.observe("lat", 2)                 # 默认服务、无标签
t.observe("lat", 4, labels=(("k", "v"),))               # 默认服务、带标签
t.observe("lat", 8, service="api"); t.observe("lat", 16, service="api")

t.histogram_summary("lat", [2, 8])
# {'boundaries': [2, 8], 'counts': [2, 2, 1], 'count': 5}
# 五条序列值按快照顺序（服务、名称、标签）拼接，左开右闭分桶

t.histogram_summary("lat", [2, 8], service="")          # 只查默认服务
# {'boundaries': [2, 8], 'counts': [2, 1, 0], 'count': 3}
t.histogram_summary("lat", [2, 8], labels=(), service="api")
# {'boundaries': [2, 8], 'counts': [0, 1, 1], 'count': 2}：显式空标签只命中无标签
t.histogram_summary("missing", [1, 2])                  # None：没有同名非空序列
t.histogram_summary("lat", [])                          # ValueError：boundaries 必须非空
t.histogram_summary("lat", [8, 2])                      # ValueError：必须严格递增
t.histogram_summary("lat", [2, True])                   # ValueError：bool 不是合法边界
t.histogram_summary("lat", [2], service=1)              # ValueError：service 必须是字符串
t.histogram_summary(["lat"], [2])                       # ValueError：name 必须可哈希
```

## span_duration_stats 示例

```python
t = Telemetry(iter(range(100)).__next__)
t.start("a"); t.finish("a", error="boom")   # start 0, end 1
t.start("b", service="api"); t.finish("b", service="api")  # start 2, end 3
t.start("open", service="api")              # 未结束，不参与统计

t.span_duration_stats()
# {'values': [1.0, 1.0], 'count': 2, 'sum': 2.0,
#  'minimum': 1.0, 'maximum': 1.0, 'mean': 1.0}
# values 按服务、开始时间、标识排序（默认服务 a 在前，api b 在后）

t.span_duration_stats(status="error")       # 只含 error 非 None：仅 a
# {'values': [1.0], 'count': 1, 'sum': 1.0, 'minimum': 1.0,
#  'maximum': 1.0, 'mean': 1.0}
t.span_duration_stats(service="api")        # 服务筛选：只命中 b
t.span_duration_stats(status="open")        # ValueError：status 只能是 closed/error
Telemetry().span_duration_stats()           # None：没有已结束跨度
```

## spans_by_duration 示例

```python
t = Telemetry(iter([0,1,    # a：dur 1（默认服务）
                    2,5,    # api b：dur 3
                    4,14,   # api c：dur 10
                    15]).__next__)  # open 只有 start
t.start("a"); t.finish("a", error="boom")
t.start("b", service="api"); t.finish("b", service="api")
t.start("c", service="api"); t.finish("c", service="api")
t.start("open", service="api")              # 未结束，不转换也不返回

[e["span"] for e in t.spans_by_duration(minimum=3)]
# ['b', 'c']：闭区间 minimum <= duration，按服务、start、span 排序
[e["span"] for e in t.spans_by_duration(minimum=3, maximum=3)]
# ['b']：闭区间两侧都包含
[e["span"] for e in t.spans_by_duration(status="error")]
# ['a']：只含 error 非 None 的已结束跨度
row = t.spans_by_duration(service="api", labels=())[0]
# {'span': 'b', 'service': 'api', 'parent': None, 'start': 2,
#  'end': 5, 'error': None, 'duration': 3.0}
type(row["duration"])                       # <class 'float'>
t.spans_by_duration(status="open")          # ValueError：status 只能是 closed/error
t.spans_by_duration(minimum=True)           # ValueError：bool 不是合法边界
t.spans_by_duration(minimum=9, maximum=1)   # ValueError：minimum 不得大于 maximum
Telemetry().spans_by_duration()             # []：没有已结束跨度
```

## spans_by_start_time 示例

```python
t = Telemetry(iter([0, 1,    # a：start 0（默认服务），已结束
                    2, 5,    # api b：start 2，已结束
                    4]).__next__)  # api open：start 4，未结束
t.start("a"); t.finish("a", error="boom")
t.start("b", service="api"); t.finish("b", service="api")
t.start("open", service="api")              # 未结束，缺省 status 也包含

[e["span"] for e in t.spans_by_start_time(minimum=2)]
# ['b', 'open']：闭区间 minimum <= start，按服务、start、span 排序
[e["span"] for e in t.spans_by_start_time(minimum=2, maximum=4)]
# ['b', 'open']：闭区间两侧都包含（start=4 也命中）
[e["span"] for e in t.spans_by_start_time(status="closed")]
# ['a', 'b']：status=closed 时未结束的 open 被排除
[e["span"] for e in t.spans_by_start_time(status="open")]
# ['open']：只保留未结束跨度，仅校验其 start
t.spans_by_start_time(service="api", labels=(), minimum=3)
# [{'span': 'open', 'service': 'api', 'parent': None, 'start': 4,
#   'end': None, 'error': None}]  # 与 query 同形，不附加派生字段
t.spans_by_start_time(status="done")        # ValueError：status 只能是 open/closed/error
t.spans_by_start_time(minimum=True)         # ValueError：bool 不是合法边界
t.spans_by_start_time(minimum=9, maximum=1) # ValueError：minimum 不得大于 maximum
Telemetry().spans_by_start_time()           # []：没有跨度
```

