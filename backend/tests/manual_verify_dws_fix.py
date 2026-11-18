"""端到端验证：空维度/非法度量隔离、中断续写、发布阻断与恢复、分页筛选。"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.seed import SEED_ROWS
from app.store import store
from app.services.dws_layer import DwsLayerService
from app.services.data_publish import DataPublishService


def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store.reset_runtime()


def section(title):
    print(f"\n===== {title} =====")


# ---------------------------------------------------------------------------
# 1. 空维度 + 非法金额隔离
# ---------------------------------------------------------------------------
reset_store()
section("1. 空维度与非法金额隔离")
# DWD_ORD_001 -> dws_order_daily，维度 dt,order_status，度量 order_amount SUM
store.set_table_rows("dwd_order_detail", [
    {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
    {"order_id": "O002", "order_amount": "200.00", "order_status": "已完成", "dt": "2026-11-01"},
    {"order_id": "O003", "order_amount": "50.00", "order_status": "待支付", "dt": "2026-11-01"},
    {"order_id": "O004", "order_amount": "abc", "order_status": "待支付", "dt": "2026-11-01"},      # 非法金额
    {"order_id": "O005", "order_amount": "80.00", "order_status": "", "dt": "2026-11-01"},          # 空维度
    {"order_id": "O006", "order_amount": None, "order_status": "  ", "dt": "2026-11-01"},           # 两者皆有
    {"order_id": "O007", "order_amount": "70.00", "order_status": "已完成", "dt": "2026-11-02"},
])
dws = DwsLayerService()
res = dws.aggregate_from_dwd(1)
assert res["ok"], res
print("聚合结果:", res["message"])
main_rows = store.table_rows("dws_order_daily")
abnormal = store.get_dws_abnormal_rows("dws_order_daily")
print("主结果分组:")
for r in main_rows:
    print("  ", r)
print("异常隔离行数:", len(abnormal))
for r in abnormal:
    print("  ", {k: r[k] for k in ("order_id", "_reason", "_empty_dims", "_invalid_measure")})

# 断言：非法/空维度不进主表，主表没有零值分组
assert len(main_rows) == 3, f"应3个合法分组，实际 {len(main_rows)}"
completed_0101 = next(r for r in main_rows if r["dt"] == "2026-11-01" and r["order_status"] == "已完成")
assert completed_0101["order_amount_agg"] == 300.0
assert completed_0101["明细总数"] == 2
assert all(r.get("order_status") not in ("", "  ") for r in main_rows)
assert len(abnormal) == 3
reasons = sorted(r["_reason"] for r in abnormal)
assert reasons == ["空维度", "空维度+非法度量", "非法度量"], reasons

entry = store.find("dws_layer", 1)
assert entry["结果版本"] == 1
assert entry["合法分组数"] == 3
assert entry["异常行数"] == 3
assert entry["异常窗口数"] >= 1
windows = dws.get_windows(1)
pending = [w for w in windows if w["状态"] == "待修复"]
print("待修复窗口数:", len(pending))
assert len(pending) >= 1
notes = store.get_notifications(module="dws_layer", only_open=True)
print("DWS通知数:", len(notes))
assert len(notes) >= 1

# ---------------------------------------------------------------------------
# 2. 发布被阻断，旧版本保持
# ---------------------------------------------------------------------------
section("2. 待修复窗口阻断数据发布")
dp = DataPublishService()
# seed 的 data_publish id=1 数据集就是 dws_order_daily，先给它一个已发布旧版本快照
store.save_published_snapshot("dws_order_daily", {
    "version": 3, "table": "dws_order_daily", "rows": [{"old": True}],
    "row_count": 1, "source_result_version": 0, "published_at": "2026-10-31T00:00:00",
})
pub_entry, msg = dp.run_action(1, "确认发布")
assert pub_entry is None, f"应被阻断，实际: {pub_entry}"
print("阻断信息:", msg)
assert "待修复窗口" in msg
# 阻断不改变发布版本状态、不产生新快照，旧版本保持
assert store.find("data_publish", 1)["status"] == "已发布"
assert store.find("data_publish", 1).get("_published_data") is None
snap = store.get_published_snapshot("dws_order_daily")
assert snap["version"] == 3 and snap["rows"] == [{"old": True}], "旧发布版本必须保持不变"
blockers = dp.pending_windows("dws_order_daily")
assert len(blockers) == len(pending)
pub_notes = store.get_notifications(module="data_publish", only_open=True)
assert len(pub_notes) >= 1
print("发布阻断通知数:", len(pub_notes))

# ---------------------------------------------------------------------------
# 3. 修复数据后重新聚合：窗口核销、通知核销、发布成功且版本递增
# ---------------------------------------------------------------------------
section("3. 修复后重新聚合并发布")
store.set_table_rows("dwd_order_detail", [
    {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
    {"order_id": "O002", "order_amount": "200.00", "order_status": "已完成", "dt": "2026-11-01"},
    {"order_id": "O003", "order_amount": "50.00", "order_status": "待支付", "dt": "2026-11-01"},
    {"order_id": "O007", "order_amount": "70.00", "order_status": "已完成", "dt": "2026-11-02"},
])
res2 = dws.aggregate_from_dwd(1)
assert res2["ok"], res2
print(res2["message"])
assert res2["result_version"] == 2, "结果版本号应递增"
assert res2["abnormal_count"] == 0
windows2 = dws.get_windows(1)
assert all(w["状态"] == "已修复" for w in windows2), "历史窗口应全部标记已修复"
assert store.get_notifications(module="dws_layer", only_open=True) == [], "DWS通知应核销无残留"
assert store.get_notifications(module="data_publish", only_open=True) == [], "发布阻断通知应核销无残留"
assert dp.pending_windows("dws_order_daily") == []
pub_entry2, msg2 = dp.run_action(1, "确认发布")
assert pub_entry2 is not None, msg2
assert pub_entry2["status"] == "已发布"
new_snap = store.get_published_snapshot("dws_order_daily")
assert new_snap["version"] == 4, f"新版本应为4，实际 {new_snap['version']}"
print("发布成功，快照版本: v", new_snap["version"], "行数:", new_snap["row_count"])
published = dp.get_published_data(1)
assert len(published["rows"]) == 3 and published["version"] == 4

# 后续再聚合产生新版本草稿，已发布快照不应漂移
store.set_table_rows("dwd_order_detail", [
    {"order_id": "O001", "order_amount": "999.00", "order_status": "已完成", "dt": "2026-11-03"},
])
dws.aggregate_from_dwd(1)
published_stable = dp.get_published_data(1)
assert published_stable["version"] == 4 and len(published_stable["rows"]) == 3, "用户只看到已发布的那一份完整结果"
print("再聚合后已发布快照保持 v4 不变 ✓")

# ---------------------------------------------------------------------------
# 4. 中断续写：只处理未完成部分
# ---------------------------------------------------------------------------
section("4. Bugfix中断后续写")
reset_store()
rows = [
    {"order_id": f"O{i:03d}", "order_amount": f"{i * 10.0:.2f}",
     "order_status": "已完成" if i % 2 else "待支付", "dt": "2026-11-01"}
    for i in range(1, 11)
]
rows.append({"order_id": "O011", "order_amount": "bad", "order_status": "待支付", "dt": "2026-11-01"})
store.set_table_rows("dwd_order_detail", rows)

# 中断场景
r1 = dws.aggregate_from_dwd(1, fail_after=4)
assert r1.get("interrupted"), r1
print(r1["message"])
assert r1["processed"] == 4 and r1["total"] == 11
run = dws.get_run(1)
assert run["status"] == "中断" and len(run["processed_indexes"]) == 4

# 未续写直接重新聚合应被拒绝
r_reject = dws.aggregate_from_dwd(1)
assert r_reject.get("interrupted") and not r_reject["ok"]
print("重复全量聚合被拒绝 ✓:", r_reject["message"][:30], "...")

# 续写一段后仍中断
r2 = dws.aggregate_from_dwd(1, resume=True, fail_after=3)
assert r2.get("interrupted") and r2["processed"] == 7
# 续写完成
r3 = dws.aggregate_from_dwd(1, resume=True)
assert r3["ok"], r3
print(r3["message"])
final_rows = store.table_rows("dws_order_daily")
# 全量参照结果
from app.services.aggregation import safe_aggregate
full_result, full_abnormal = safe_aggregate(rows, ["dt", "order_status"], "order_amount", "SUM")
assert len(final_rows) == len(full_result) == 2
assert len(store.get_dws_abnormal_rows("dws_order_daily")) == len(full_abnormal) == 1
total_sum = sum(r["order_amount_agg"] for r in final_rows)
assert abs(total_sum - sum(i * 10.0 for i in range(1, 11))) < 1e-9
print("续写结果与全量聚合一致，度量合计:", total_sum)

# ---------------------------------------------------------------------------
# 5. 结果表筛选/排序/翻页
# ---------------------------------------------------------------------------
section("5. 筛选、排序、翻页复核")
page1, total, cols = dws.query_rows("dws_order_daily", page=1, size=1)
page2, _, _ = dws.query_rows("dws_order_daily", page=2, size=1)
assert total == 2 and len(page1) == 1 and len(page2) == 1 and page1 != page2
print("翻页: total=%d, 两页内容不同 ✓" % total)
sorted_desc, _, _ = dws.query_rows("dws_order_daily", sort_by="order_amount_agg", sort_order="desc")
assert sorted_desc[0]["order_amount_agg"] >= sorted_desc[-1]["order_amount_agg"]
print("降序排序 ✓")
filtered, ftotal, _ = dws.query_rows("dws_order_daily", filters={"order_status": "已完成"})
assert ftotal == 1 and filtered[0]["order_status"] == "已完成"
kw_rows, kw_total, _ = dws.query_rows("dws_order_daily", keyword="待支付")
assert kw_total == 1 and kw_rows[0]["order_status"] == "待支付"
print("字段过滤与关键字检索 ✓")
ab_page, ab_total, ab_cols = dws.query_rows(
    "dws_order_daily", kind="abnormal", filters={"_reason": "非法度量"})
assert ab_total == 1 and ab_page[0]["order_id"] == "O011"
print("异常隔离表筛选 ✓，列:", ab_cols)

print("\n全部端到端断言通过 ✅")
