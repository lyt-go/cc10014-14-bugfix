"""发布版本业务规则：从transform_dag/DWS获取最新输出，创建可消费的发布版本。

与汇总层（DWS）的跨模块联动契约：
1. 确认发布前除质量稽核外，还要检查目标DWS数据集是否存在「待修复窗口」（空维度/
   非法度量被隔离的统计窗口）；存在则阻断发布，已发布旧版本保持不变，发布版本停留
   在待校验，并生成跨模块通知，避免错误零值被当成真实度量发布。
2. 发布成功时对当前DWS主结果表做版本化快照，新版本号递增；同一数据集用户始终只
   看到一份完整的DWS聚合结果（当前已发布快照）。
3. 待修复窗口修复后（DWS侧重新聚合且窗口消失、通知核销），即可重新发布。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "data_publish"
LIST_FIELDS = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
REQUIRED_FIELDS = ["版本编号", "数据集名称"]
STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []


def _now() -> str:
    return datetime.now().isoformat()


class DataPublishService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("版本编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("来源转换节点"):
            entry["来源转换节点"] = values["来源转换节点"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"发布版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据发布可执行范围"
        # 质量稽核→数据发布：确认发布前必须通过质量校验
        if action == "确认发布":
            dataset = entry.get("数据集名称", "")
            qc_result = self._check_quality(dataset)
            if not qc_result.get("passed", False):
                return None, f"数据发布被阻断：{qc_result.get('message', '质量稽核未通过')}"
            # DWS待修复窗口校验：异常数据隔离期间禁止发布，旧版本保持不变
            blockers = self.pending_windows(dataset)
            if blockers:
                self._notify_blocked(entry, dataset, blockers)
                detail = "；".join(
                    f"窗口 {w.get('window_key') or '（空维度）'}（{w.get('异常原因')}，{w.get('异常行数')}条）"
                    for w in blockers[:5])
                return None, (
                    f"数据发布被阻断：数据集 {dataset} 存在 {len(blockers)} 个待修复窗口（{detail}）。"
                    "异常记录已隔离未混入主结果，请先在汇总层修复并重新聚合；当前已发布旧版本保持不变。"
                )
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        if action == "确认发布":
            self._attach_latest_output(entry)
        return entry, f"发布版本已{action}"

    # ------------------------------------------------------------------
    # DWS联动：待修复窗口、阻断通知
    # ------------------------------------------------------------------

    def pending_windows(self, dataset: str) -> list[dict[str, Any]]:
        """返回目标数据集当前所有待修复窗口；没有DWS表时返回空列表（不阻断）。"""
        dws_entry = None
        for row in store.rows("dws_layer"):
            if row.get("表名称") == dataset or row.get("表编号") == dataset:
                dws_entry = row
                break
        if not dws_entry:
            return []
        table_name = dws_entry.get("表名称", dataset)
        return [w for w in store.get_dws_windows(table_name) if w.get("状态") == "待修复"]

    def _notify_blocked(self, entry: dict[str, Any], dataset: str,
                        blockers: list[dict[str, Any]]) -> None:
        now = _now()
        open_refs = {n.get("ref") for n in store.get_notifications(module=MODULE, only_open=True)}
        for window in blockers:
            ref = f"publish:{entry.get('版本编号')}:{dataset}:{window.get('window_key')}"
            if ref in open_refs:
                continue
            store.add_notification({
                "module": MODULE,
                "scope": "publish_block",
                "ref": ref,
                "level": "阻断",
                "title": f"发布版本 {entry.get('版本编号')} 因待修复窗口被阻断",
                "message": f"数据集 {dataset} 窗口 {window.get('window_key') or '（空维度）'} 待修复，发布已拦截",
                "dataset": dataset,
                "created_at": now,
            })

    def _check_quality(self, dataset: str) -> dict[str, Any]:
        """检查数据集是否通过所有质量稽核规则。"""
        from app.services.quality_check import QualityCheckService
        qc = QualityCheckService()
        # 查找该数据集的所有生效稽核规则
        rules = []
        for row in store.rows("quality_check"):
            if row.get("校验表名") == dataset and row.get("status") == "已生效":
                rules.append(row)
        if not rules:
            # 没有规则默认通过，但记录日志
            return {"passed": True, "message": "无生效稽核规则，默认通过"}
        for rule in rules:
            result = qc.execute_check(rule["id"])
            res = result.get("result", {})
            if res.get("failed", 0) > 0:
                return {"passed": False, "message": f"规则 {rule.get('规则编号')} 稽核失败 {res.get('failed')} 条"}
        return {"passed": True, "message": f"全部 {len(rules)} 条稽核规则通过"}

    # ------------------------------------------------------------------
    # 发布快照：同一数据集用户始终只看到一份完整结果，版本号递增
    # ------------------------------------------------------------------

    def _resolve_output_table(self, entry: dict[str, Any]) -> str:
        dataset = entry.get("数据集名称", "")
        node_code = entry.get("来源转换节点", "")
        if node_code:
            for row in store.rows("transform_dag"):
                if row.get("节点编号") == node_code:
                    return row.get("输出表名", "") or dataset
        return dataset

    def _attach_latest_output(self, entry: dict[str, Any]) -> None:
        dataset = entry.get("数据集名称", "")
        output_table = self._resolve_output_table(entry)
        data = store.table_rows(output_table)
        previous = store.get_published_snapshot(dataset)
        version = int(previous["version"]) + 1 if previous else 1
        # 关联DWS表的结果版本号，方便双向追溯
        source_version = None
        for row in store.rows("dws_layer"):
            if row.get("表名称") == output_table:
                source_version = row.get("结果版本")
                break
        snapshot = {
            "version": version,
            "table": output_table,
            "rows": [dict(r) for r in data],
            "row_count": len(data),
            "source_result_version": source_version,
            "published_at": _now(),
        }
        store.save_published_snapshot(dataset, snapshot)
        entry["_published_data"] = {
            "table": output_table,
            "row_count": len(data),
            "published_version": version,
            "published_at": snapshot["published_at"],
        }

    def get_published_data(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        dataset = entry.get("数据集名称", "")
        snapshot = store.get_published_snapshot(dataset)
        if snapshot:
            # 用户只看到当前已发布的那一份完整结果（快照，不随后续草稿聚合漂移）
            return {
                "table": snapshot["table"],
                "version": snapshot["version"],
                "published_at": snapshot["published_at"],
                "rows": [dict(r) for r in snapshot["rows"]],
            }
        # 兼容：尚未产生过快照时回退到关联转换节点的当前输出
        node_code = entry.get("来源转换节点", "")
        if not node_code:
            return None
        for row in store.rows("transform_dag"):
            if row.get("节点编号") == node_code:
                return {
                    "table": row.get("输出表名", ""),
                    "version": 0,
                    "published_at": None,
                    "rows": store.table_rows(row.get("输出表名", "")),
                }
        return None

    def query_published_rows(
        self,
        entry_id: int,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        sort_by: str | None = None,
        sort_order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, list[str], dict[str, Any]]:
        """对已发布结果做条件筛选、排序与翻页，分页定位与检索条件可复核。"""
        published = self.get_published_data(entry_id)
        if not published:
            return [], 0, [], {}
        rows = [dict(r) for r in published["rows"]]
        columns = sorted({key for row in rows for key in row.keys()})
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            kw = keyword.casefold()
            rows = [row for row in rows if any(kw in str(value).casefold() for value in row.values())]
        if sort_by and sort_by in columns:
            def sort_key(row: dict[str, Any]) -> tuple[int, Any]:
                value = row.get(sort_by)
                try:
                    return (0, float(value))
                except (TypeError, ValueError):
                    return (1, str(value if value is not None else ""))
            rows = sorted(rows, key=sort_key, reverse=sort_order.lower() == "desc")
        total = len(rows)
        start = (page - 1) * size
        meta = {k: published[k] for k in ("table", "version", "published_at") if k in published}
        return rows[start:start + size], total, columns, meta
