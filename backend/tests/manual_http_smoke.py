"""HTTP 冒烟测试：对真实运行的 uvicorn 服务发请求，验证端到端接口契约。

服务内为内存仓库，脚本按操作顺序推进数据，不依赖外部重置。
用法：先启动服务（端口8011），再 python -I tests/manual_http_smoke.py
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000")


def call(method: str, path: str, payload=None):
    if "?" in path:
        base, query = path.split("?", 1)
        path = base + "?" + urllib.parse.quote(query, safe="=&?%")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # noqa: F821 - urllib.error在运行时可用
        return exc.code, json.loads(exc.read().decode())


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


# 服务启动时 seed 的 dwd_order_detail 无模拟表数据，聚合会自动从ODS构建5行mock数据。
# 先通过聚合接口触发一次基线聚合（mock数据全部合法）。
print("[1] 基线聚合（自动构建DWD）")
status, body = call("POST", "/api/dws_layer/1/aggregate")
check(status == 200 and body["ok"], f"聚合成功: {body.get('message')}")
check(body["abnormal_count"] == 0, "mock基线数据无异常")
version1 = body["result_version"]

print("[2] 主结果分页/排序参数生效")
status, data = call("GET", "/api/dws_layer/1/data?page=1&size=1&sort_by=order_amount_agg&sort_order=desc")
check(status == 200 and data["total"] >= 1, f"分页返回 total={data['total']}")
check(len(data["items"]) == 1, "size=1 当前页1行")
check("明细总数" in data["columns"], "列含明细总数")

status, page2 = call("GET", "/api/dws_layer/1/data?page=2&size=1")
check(page2["page"] == 2, "翻页定位到第2页")

print("[3] 列表筛选（汇总表管理列表）")
status, lst = call("GET", "/api/dws_layer?表编号=DWS_ORD_001")
check(lst["total"] == 1 and lst["items"][0]["表编号"] == "DWS_ORD_001", "按表编号字段过滤")
status, lst = call("GET", "/api/dws_layer?keyword=DWS&status=草稿&page=1&size=10")
check(lst["total"] == 2, f"关键字+状态过滤命中2条草稿，实际{lst['total']}")

print("[4] 窗口/通知/运行态空态")
status, w = call("GET", "/api/dws_layer/1/windows")
check(w["pending"] == 0, "无待修复窗口")
status, n = call("GET", "/api/dws_layer/notifications")
check(n["total"] == 0, "无残留通知")
status, run = call("GET", "/api/dws_layer/1/run")
check(run["has_run"] and run["run"]["status"] == "已完成", "运行态为已完成")

print("[5] 中断与续写（fail_after演练）")
status, interrupted = call("POST", "/api/dws_layer/1/aggregate?fail_after=2")
check(interrupted.get("interrupted") and interrupted["processed"] == 2,
      f"处理2行中断: {interrupted.get('message')}")
status, run2 = call("GET", "/api/dws_layer/1/run")
check(run2["run"]["status"] == "中断", "运行态标记中断")
# 中断态拒绝重新全量聚合
status, reject = call("POST", "/api/dws_layer/1/aggregate")
check(reject.get("interrupted") and reject["ok"] is False, "中断态拒绝全量重跑")
# 续写
status, resumed = call("POST", "/api/dws_layer/1/aggregate/resume")
check(status == 200 and resumed["ok"], f"续写完成: {resumed.get('message')}")
check(resumed["result_version"] == version1 + 1, "续写完成后版本号递增")
# 再次续写应被拒绝
status, no_resume = call("POST", "/api/dws_layer/1/aggregate/resume")
check(no_resume["ok"] is False, "无中断任务时续写被拒绝")

print("[6] 发布链路：无窗口时发布成功，快照可分页查询")
status, pub = call("POST", "/api/data_publish/1/actions", {"action": "确认发布"})
check(pub["ok"], f"无待修复窗口发布成功: {pub.get('message')}")
status, blockers = call("GET", "/api/data_publish/1/blockers")
check(blockers["blocked"] is False, "阻断窗口为空")
status, pubdata = call("GET", "/api/data_publish/1/data?page=1&size=2")
check(pubdata["version"] >= 1 and pubdata["table"] == "dws_order_daily",
      f"已发布快照 v{pubdata.get('version')} 来源表正确")
check("items" in pubdata and pubdata["total"] >= 1, "发布数据支持分页结构")
status, pubnote = call("GET", "/api/data_publish/notifications")
check(pubnote["total"] == 0, "发布页无残留通知")

print("\nHTTP 冒烟测试全部通过 ✅")
