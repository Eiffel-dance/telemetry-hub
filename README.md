# Telemetry Hub

A dependency-free Python reference implementation for observability, metrics, tracing.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个轻量级本地遥测聚合器，统一记录计数器、数值样本和带父子关系的跨度，并输出稳定排序的 JSON 快照。聚合必须区分服务和标签，浮点统计采用公开规则，未结束跨度和异常状态要可查询；整个组件不发送网络请求，快照可直接用于离线诊断和回放。

## API

- `inc(name, value=1, labels=(), service=None)` / `observe(name, value, labels=(), service=None)`：可选 `service`，缺省归一化为空字符串（默认服务），显式服务名必须是非空字符串；同名同标签但服务不同分别聚合。标签按键字典序归一化，重复键或不可 JSON 序列化的标签抛 `ValueError`，且不改变已有聚合。
- 样本保留按写入顺序的原值 `values`；快照对非空样本附 `count`、`sum`（按写入顺序累加）、`minimum`、`maximum`、`mean`（`sum/count`，不四舍五入）。NaN/无穷值抛 `ValueError`，空样本不产生统计。
- `start(span, parent=None, service=None)` / `finish(span, error=None, service=None)`：service 与跨度标识共同定位跨度；父标识原样保留。
- `query(status)`：仅接受 `'open'`（`end` 为空）和 `'error'`（已结束且 `error` 非空），其他值抛 `ValueError`，无匹配返回 `[]`；结果含 `span/service/parent/start/end/error`，按服务、开始时间、标识排序。
- `snapshot()` / `json()`：计数器和样本按服务、名称、标签排序，跨度按服务、开始时间、标识排序；JSON 紧凑（`separators=(",", ":")`）且键序稳定（`sort_keys=True`）。
- `Telemetry.from_snapshot(payload, clock=time.time)`：离线把 `snapshot()` 字典或 `json()` 文本重建为独立实例，计数器、样本原值与跨度的 parent/start/end/error 全部带回，样本统计按公开浮点规则从 values 重算（输入中存在的统计字段须与重算一致，空 values 不得携带统计）。payload 顶层只能含 `counters`、`samples`、`spans` 三个数组；缺字段、多字段、非法 JSON、重复记录、无效标签、不可哈希跨度标识、不可严格 JSON 表示的值等一律抛 `ValueError`，且失败前不留下半成品实例、不改动已有实例。恢复过程不联网、不读写文件、不修改输入；恢复后与原对象互不共享数据，`open` 跨度可继续用 `finish()` 结束。
- `merge_snapshot(payload)`：把一份离线分片快照原子合并进当前实例，返回 `None`；输入接受范围与 `from_snapshot` 相同（字典、JSON 文本、UTF-8 字节），校验规则也一致。计数器按服务、名称、归一化标签定位，相同键按加法语义累加，不可相加抛 `ValueError`；相同键样本先保留当前 `values` 再按输入顺序追加输入值，统计按公开浮点规则从完整 values 重算；跨度以 service 与 span 联合定位，仅一方出现时整体复制，两方完全一致时幂等，不一致时抛 `ValueError`。先完整解析校验、再一次性提交：任何恢复或合并错误都使当前实例的聚合、跨度与时钟配置保持不变，输入不被改写，合并后数据不与 payload 共享可变对象；不联网、不读写文件。

