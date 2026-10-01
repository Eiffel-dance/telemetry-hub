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

