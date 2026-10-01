# Telemetry Hub

A dependency-free Python reference implementation for observability, metrics, tracing.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个轻量级本地遥测聚合器，统一记录计数器、数值样本和带父子关系的跨度，并输出稳定排序的 JSON 快照。聚合必须区分服务和标签，浮点统计采用公开规则，未结束跨度和异常状态要可查询；整个组件不发送网络请求，快照可直接用于离线诊断和回放。
