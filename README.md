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
- `Telemetry.from_snapshot(payload)`：从 `snapshot()` 返回的对象或 `json()` 返回的紧凑 JSON 文本离线重建独立的 `Telemetry` 实例。计数器、样本原始 `values` 和跨度 `parent/start/end/error` 全部带回，样本统计按 `values` 重算（输入若带统计字段必须与重算一致，空 `values` 不得带统计）；恢复后 `snapshot()`/`json()`/`query` 结果与有效输入一致，open 跨度可继续 `finish()`。payload 顶层只能含 `counters`、`samples`、`spans` 三个数组，记录字段不得缺失或多余、键不得重复，服务/标签/名称/跨度标识/时间值必须可严格 JSON 表示（跨度标识还须可哈希）；缺少字段、非法 JSON、重复记录、统计不一致、标签无效等一律抛 `ValueError`，且失败不留半成品实例、不修改输入，成功后输入与新实例互不共享列表和记录。`snapshot()` 对象中不可严格 JSON 表示的 `error`（如异常对象）不能直接恢复，需改用 `json()` 的结果（error 已被替换为 `{"type", "message"}` 占位对象）。

