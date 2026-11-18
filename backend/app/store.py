"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # DWS聚合运行态: "dws表名" -> 最近一次运行元数据
        self._dws_runs: dict[str, dict[str, Any]] = {}
        # DWS待修复窗口清单: "dws表名" -> [窗口记录, ...]（含历史，按状态区分）
        self._dws_windows: dict[str, list[dict[str, Any]]] = {}
        # DWS异常分组隔离表数据: "dws表名__abnormal" -> [异常行, ...]
        # 已发布快照: "数据集名称" -> {version, rows, published_at, source_version}
        self._published_snapshots: dict[str, dict[str, Any]] = {}
        # 跨模块通知（异常窗口、发布阻断等），修复后自动核销避免残留
        self._notifications: list[dict[str, Any]] = []

    def reset_runtime(self) -> None:
        """清空所有运行期状态，供测试回到干净起点（模块登记数据由调用方自行重置）。"""
        self._table_data.clear()
        self._versions.clear()
        self._lineage_edges.clear()
        self._quality_results.clear()
        self._snapshot_data.clear()
        self._archive_records.clear()
        self._backfill_partitions.clear()
        self._dws_runs.clear()
        self._dws_windows.clear()
        self._published_snapshots.clear()
        self._notifications.clear()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))

    # ---- DWS聚合运行态与待修复窗口 ----

    def get_dws_run(self, table_name: str) -> dict[str, Any] | None:
        run = self._dws_runs.get(table_name)
        return dict(run) if run else None

    def save_dws_run(self, table_name: str, run: dict[str, Any]) -> None:
        self._dws_runs[table_name] = dict(run)

    def get_dws_windows(self, table_name: str) -> list[dict[str, Any]]:
        return [dict(w) for w in self._dws_windows.get(table_name, [])]

    def save_dws_windows(self, table_name: str, windows: list[dict[str, Any]]) -> None:
        self._dws_windows[table_name] = [dict(w) for w in windows]

    def dws_abnormal_table(self, table_name: str) -> str:
        return f"{table_name}__abnormal"

    def get_dws_abnormal_rows(self, table_name: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.table_rows(self.dws_abnormal_table(table_name))]

    # ---- 发布快照（同一数据集用户始终只看到一份完整结果） ----

    def save_published_snapshot(self, dataset: str, snapshot: dict[str, Any]) -> None:
        self._published_snapshots[dataset] = dict(snapshot)

    def get_published_snapshot(self, dataset: str) -> dict[str, Any] | None:
        snapshot = self._published_snapshots.get(dataset)
        return dict(snapshot) if snapshot else None

    # ---- 跨模块通知 ----

    def add_notification(self, notification: dict[str, Any]) -> dict[str, Any]:
        note = {"id": len(self._notifications) + 1, "status": "待处理", **notification}
        self._notifications.append(note)
        return note

    def get_notifications(self, module: str | None = None,
                          scope: str | None = None, only_open: bool = False) -> list[dict[str, Any]]:
        notes = self._notifications
        if module:
            notes = [n for n in notes if n.get("module") == module]
        if scope:
            notes = [n for n in notes if n.get("scope") == scope]
        if only_open:
            notes = [n for n in notes if n.get("status") == "待处理"]
        return [dict(n) for n in notes]

    def resolve_notifications(self, scope: str, ref: str) -> int:
        """核销某条数据（按 scope+ref 定位）的全部待处理通知，返回核销条数。"""
        count = 0
        for note in self._notifications:
            if note.get("scope") == scope and note.get("ref") == ref and note.get("status") == "待处理":
                note["status"] = "已核销"
                note["resolved_at"] = datetime.now().isoformat()
                count += 1
        return count

    def resolve_dataset_notifications(self, scope: str, dataset: str) -> int:
        """核销某数据集下指定scope的全部待处理通知（窗口修复后清理发布阻断残留）。"""
        count = 0
        for note in self._notifications:
            if (note.get("scope") == scope and note.get("dataset") == dataset
                    and note.get("status") == "待处理"):
                note["status"] = "已核销"
                note["resolved_at"] = datetime.now().isoformat()
                count += 1
        return count


store = Store()
