"""Bugfix回归测试：DWS按维度聚合时空维度与非法度量的隔离、中断续写与发布联动。

覆盖验收要点：
- 空维度/非法金额不再被当成零值分组，合法与异常分别落库；
- 待修复窗口清单与跨模块通知，修复后自动核销无残留；
- 发布阻断时旧版本保持，修复后发布新版本号，用户只看到一份完整结果；
- 聚合中断后只续写未完成部分，结果与全量聚合一致；
- 主结果/异常表/发布结果均支持筛选、排序、翻页复核。
"""
from __future__ import annotations

from app.seed import SEED_ROWS
from app.services.aggregation import safe_aggregate
from app.services.data_publish import DataPublishService
from app.services.dws_layer import DwsLayerService
from app.services.transform_dag import TransformDagService
from app.store import store


def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store.reset_runtime()


def _dirty_order_rows():
    return [
        {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
        {"order_id": "O002", "order_amount": "200.00", "order_status": "已完成", "dt": "2026-11-01"},
        {"order_id": "O003", "order_amount": "50.00", "order_status": "待支付", "dt": "2026-11-01"},
        {"order_id": "O004", "order_amount": "abc", "order_status": "待支付", "dt": "2026-11-01"},
        {"order_id": "O005", "order_amount": "80.00", "order_status": "", "dt": "2026-11-01"},
        {"order_id": "O006", "order_amount": None, "order_status": "  ", "dt": "2026-11-01"},
        {"order_id": "O007", "order_amount": "70.00", "order_status": "已完成", "dt": "2026-11-02"},
    ]


class TestDwsIsolation:
    def setup_method(self):
        reset_store()

    def test_empty_dimension_and_invalid_measure_are_isolated(self):
        store.set_table_rows("dwd_order_detail", _dirty_order_rows())
        dws = DwsLayerService()
        result = dws.aggregate_from_dwd(1)
        assert result["ok"]
        # 主结果只有合法分组，不出现零值/空维度分组
        main_rows = store.table_rows("dws_order_daily")
        assert len(main_rows) == 3
        assert all(str(r.get("order_status", "")).strip() for r in main_rows)
        completed = next(r for r in main_rows
                         if r["dt"] == "2026-11-01" and r["order_status"] == "已完成")
        assert completed["order_amount_agg"] == 300.0
        assert completed["明细总数"] == 2
        # 异常记录单独落库，原因可复核
        abnormal = store.get_dws_abnormal_rows("dws_order_daily")
        assert len(abnormal) == 3
        reasons = sorted(r["_reason"] for r in abnormal)
        assert reasons == ["空维度", "空维度+非法度量", "非法度量"]

    def test_safe_aggregate_never_falls_back_to_zero(self):
        result, abnormal = safe_aggregate(
            _dirty_order_rows(), ["dt", "order_status"], "order_amount", "SUM")
        assert sum(r["order_amount_agg"] for r in result) == 420.0
        assert len(abnormal) == 3
        # COUNT 不依赖度量可解析性：空度量不算非法，空维度仍隔离
        count_rows, count_abnormal = safe_aggregate(
            [{"id": "a", "user_level": "VIP", "dt": "2026-11-01"},
             {"id": "b", "user_level": "", "dt": "2026-11-01"},
             {"id": "c", "user_level": "普通", "dt": "2026-11-01"}],
            ["dt", "user_level"], "user_count", "COUNT")
        assert len(count_rows) == 2
        assert len(count_abnormal) == 1 and count_abnormal[0]["_reason"] == "空维度"

    def test_windows_and_notifications_resolved_after_fix(self):
        store.set_table_rows("dwd_order_detail", _dirty_order_rows())
        dws = DwsLayerService()
        dws.aggregate_from_dwd(1)
        assert len(dws.get_windows(1)) >= 1
        assert store.get_notifications(module="dws_layer", only_open=True)
        # 修复：DWD只剩合法记录
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O007", "order_amount": "70.00", "order_status": "已完成", "dt": "2026-11-02"},
        ])
        second = dws.aggregate_from_dwd(1)
        assert second["result_version"] == 2 and second["abnormal_count"] == 0
        assert all(w["状态"] == "已修复" for w in dws.get_windows(1))
        assert store.get_notifications(module="dws_layer", only_open=True) == []
        assert store.get_dws_abnormal_rows("dws_order_daily") == []


class TestPublishBlocking:
    def setup_method(self):
        reset_store()

    def test_publish_blocked_while_window_pending_and_old_version_kept(self):
        store.set_table_rows("dwd_order_detail", _dirty_order_rows())
        DwsLayerService().aggregate_from_dwd(1)
        dp = DataPublishService()
        store.save_published_snapshot("dws_order_daily", {
            "version": 3, "table": "dws_order_daily", "rows": [{"old": True}],
            "row_count": 1, "source_result_version": 0, "published_at": "2026-10-31T00:00:00",
        })
        entry, msg = dp.run_action(1, "确认发布")
        assert entry is None and "待修复窗口" in msg
        # 旧快照原样保留，不产生新版本
        snap = store.get_published_snapshot("dws_order_daily")
        assert snap["version"] == 3 and snap["rows"] == [{"old": True}]
        assert store.get_notifications(module="data_publish", only_open=True)

    def test_publish_succeeds_with_new_version_after_fix(self):
        store.set_table_rows("dwd_order_detail", _dirty_order_rows())
        dws = DwsLayerService()
        dws.aggregate_from_dwd(1)
        dp = DataPublishService()
        assert dp.pending_windows("dws_order_daily")
        # 修复并重新聚合
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O002", "order_amount": "200.00", "order_status": "已完成", "dt": "2026-11-01"},
        ])
        dws.aggregate_from_dwd(1)
        entry, msg = dp.run_action(1, "确认发布")
        assert entry is not None, msg
        assert entry["status"] == "已发布"
        snap = store.get_published_snapshot("dws_order_daily")
        assert snap["version"] == 1 and snap["row_count"] == 1
        # 通知全部核销
        assert store.get_notifications(module="data_publish", only_open=True) == []
        # 之后再次聚合不影响已发布快照，用户只看到一份完整结果
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O009", "order_amount": "1.00", "order_status": "已完成", "dt": "2026-11-09"},
        ])
        dws.aggregate_from_dwd(1)
        published = dp.get_published_data(1)
        assert published["version"] == 1 and len(published["rows"]) == 1


class TestTransformDagSafeAggregation:
    def setup_method(self):
        reset_store()

    def test_transform_node_isolates_dirty_rows(self):
        """转换编排的 GROUP BY 节点同样不得把空维度/非法度量当成零值。"""
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O002", "order_amount": "200", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O003", "order_amount": "bad", "order_status": "待支付", "dt": "2026-11-01"},
            {"order_id": "O004", "order_amount": "80", "order_status": "", "dt": "2026-11-01"},
        ])
        result = TransformDagService().execute_node(1)
        assert result["ok"]
        output = store.table_rows("dws_order_daily")
        # 合法只有「已完成」一组，度量 300，没有零值/空维度分组
        assert len(output) == 1
        assert output[0]["order_status"] == "已完成"
        assert output[0]["order_amount_agg"] == 300.0
        # 异常记录隔离到独立表
        abnormal = store.table_rows("dws_order_daily__abnormal")
        assert len(abnormal) == 2
        assert {r["_reason"] for r in abnormal} == {"空维度", "非法度量"}


class TestResumeAndPaging:
    def setup_method(self):
        reset_store()

    def test_interrupted_aggregate_only_resumes_remaining_rows(self):
        rows = [
            {"order_id": f"O{i:03d}", "order_amount": f"{i * 10.0:.2f}",
             "order_status": "已完成" if i % 2 else "待支付", "dt": "2026-11-01"}
            for i in range(1, 11)
        ]
        rows.append({"order_id": "O011", "order_amount": "bad",
                     "order_status": "待支付", "dt": "2026-11-01"})
        store.set_table_rows("dwd_order_detail", rows)
        dws = DwsLayerService()
        first = dws.aggregate_from_dwd(1, fail_after=4)
        assert first["interrupted"] and first["processed"] == 4
        # 中断态拒绝重新全量聚合，强制走续写
        assert dws.aggregate_from_dwd(1).get("interrupted")
        second = dws.aggregate_from_dwd(1, resume=True, fail_after=3)
        assert second["interrupted"] and second["processed"] == 7
        finished = dws.aggregate_from_dwd(1, resume=True)
        assert finished["ok"]
        # 续写结果与一次性全量安全聚合完全一致
        full_result, full_abnormal = safe_aggregate(
            rows, ["dt", "order_status"], "order_amount", "SUM")
        final_rows = store.table_rows("dws_order_daily")
        assert len(final_rows) == len(full_result)
        assert abs(sum(r["order_amount_agg"] for r in final_rows)
                   - sum(r["order_amount_agg"] for r in full_result)) < 1e-9
        assert len(store.get_dws_abnormal_rows("dws_order_daily")) == len(full_abnormal) == 1

    def test_result_tables_support_filter_sort_and_paging(self):
        store.set_table_rows("dwd_order_detail", _dirty_order_rows())
        dws = DwsLayerService()
        dws.aggregate_from_dwd(1)
        # 翻页
        page1, total, _ = dws.query_rows("dws_order_daily", page=1, size=2)
        page2, _, _ = dws.query_rows("dws_order_daily", page=2, size=2)
        assert total == 3 and len(page1) == 2 and len(page2) == 1
        # 排序
        desc, _, _ = dws.query_rows("dws_order_daily", sort_by="order_amount_agg", sort_order="desc")
        assert desc[0]["order_amount_agg"] >= desc[-1]["order_amount_agg"]
        # 字段过滤 + 关键字
        filtered, ftotal, _ = dws.query_rows("dws_order_daily", filters={"order_status": "已完成"})
        assert ftotal == 2 and all(r["order_status"] == "已完成" for r in filtered)
        kw_rows, kw_total, _ = dws.query_rows("dws_order_daily", keyword="待支付")
        assert kw_total == 1
        # 异常隔离表同样可筛选（子串匹配：「非法度量」同时命中纯非法与组合异常）
        ab_rows, ab_total, _ = dws.query_rows(
            "dws_order_daily", kind="abnormal", filters={"_reason": "非法度量"})
        assert ab_total == 2 and {r["order_id"] for r in ab_rows} == {"O004", "O006"}
        exact_rows, exact_total, _ = dws.query_rows(
            "dws_order_daily", kind="abnormal", filters={"_reason": "空维度"})
        assert exact_total == 2

    def test_published_rows_support_paging_query(self):
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O002", "order_amount": "200.00", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O003", "order_amount": "50.00", "order_status": "待支付", "dt": "2026-11-01"},
        ])
        dws = DwsLayerService()
        dws.aggregate_from_dwd(1)
        dp = DataPublishService()
        dp.run_action(1, "确认发布")
        rows, total, cols, meta = dp.query_published_rows(
            1, filters={"order_status": "已完成"}, page=1, size=10)
        assert total == 1 and rows[0]["order_amount_agg"] == 300.0
        assert meta["version"] == 1 and meta["table"] == "dws_order_daily"
        assert "order_amount_agg" in cols
