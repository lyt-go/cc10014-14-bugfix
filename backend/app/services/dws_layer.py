"""汇总层管理业务规则：消费DWD明细数据，按维度聚合生成DWS汇总表。

本次修复的核心契约：
1. 空维度记录与非法度量记录不再被当成零值混入聚合，而是隔离到异常分组表，
   合法聚合结果与异常数据分别落库（主表 vs ``<表名>__abnormal``）。
2. 聚合支持中断续写：中断后运行态（已处理下标、分组累加器、异常明细）持久化，
   再次执行只处理未完成部分，已完成的分组不会重复计算。
3. 每次完整聚合生成递增的结果版本号；待修复窗口清单跨次保留，修复后自动核销，
   用户始终只看到一份完整的DWS主结果表。
4. 异常窗口会产生跨模块通知，数据发布侧据此阻断发布，避免错误零值流入发布版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.aggregation import (
    REASON_BOTH,
    REASON_EMPTY_DIMENSION,
    REASON_INVALID_MEASURE,
    is_blank_dimension,
    parse_measure,
)
from app.store import store

MODULE = "dws_layer"
LIST_FIELDS = ["表编号", "统计粒度", "汇总维度", "度量字段", "聚合函数", "时间范围", "刷新周期", "创建批次"]
REQUIRED_FIELDS = ["表编号", "统计粒度", "汇总维度"]
STATUS_ORDER = ["草稿", "待校验", "已上线", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认上线": "已上线", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []

RUN_INTERRUPTED = "中断"
RUN_FINISHED = "已完成"
WINDOW_PENDING = "待修复"
WINDOW_FIXED = "已修复"


def _now() -> str:
    return datetime.now().isoformat()


class DwsLayerService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("表编号", "")).casefold()]
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
        entry["结果版本"] = 0
        entry["聚合状态"] = "未执行"
        entry["合法分组数"] = 0
        entry["异常行数"] = 0
        entry["异常窗口数"] = 0
        if values.get("来源明细表"):
            entry["来源明细表"] = values["来源明细表"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"汇总表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于汇总层管理可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"汇总表已{action}"

    # ------------------------------------------------------------------
    # 聚合主流程
    # ------------------------------------------------------------------

    def _resolve_dwd(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        dwd_code = entry.get("来源明细表", "")
        for row in store.rows("dwd_layer"):
            if row.get("表编号") == dwd_code or row.get("表名称") == dwd_code:
                return row, ""
        return None, f"来源明细表 {dwd_code} 不存在"

    def aggregate_from_dwd(
        self,
        entry_id: int,
        *,
        resume: bool = False,
        fail_after: int | None = None,
    ) -> dict[str, Any]:
        """从关联DWD表读取数据，按汇总维度聚合；异常分组与正常聚合分别落库。

        ``resume=True`` 时只续写上次中断时尚未处理的下标；``fail_after`` 用于
        模拟中断（处理到指定条数后挂起），运行态会保留以便续写。
        """
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "汇总表不存在"}
        dwd_entry, err = self._resolve_dwd(entry)
        if not dwd_entry:
            return {"ok": False, "message": err}
        dwd_table = dwd_entry.get("表名称", "")
        dwd_data = store.table_rows(dwd_table)
        if not dwd_data:
            from app.services.dwd_layer import DwdLayerService
            DwdLayerService().build_dwd_from_ods(dwd_entry["id"])
            dwd_data = store.table_rows(dwd_table)
        if not dwd_data:
            return {"ok": False, "message": f"来源明细表 {dwd_table} 无数据可聚合"}

        dims = [d.strip() for d in entry.get("汇总维度", "").split(",") if d.strip()]
        measure = entry.get("度量字段", "")
        agg_func = entry.get("聚合函数", "SUM")
        dws_table = entry.get("表名称", f"dws_{entry_id}")

        run = store.get_dws_run(dws_table)
        if resume:
            if not run or run.get("status") != RUN_INTERRUPTED:
                return {"ok": False, "message": "没有可续写的中断任务，请直接重新执行聚合"}
            if fail_after is not None and fail_after <= 0:
                return {"ok": False, "message": "fail_after 必须为正整数"}
        else:
            if run and run.get("status") == RUN_INTERRUPTED:
                return {
                    "ok": False,
                    "message": "存在中断的聚合任务，请使用续写(resume)完成，或重新执行全量聚合",
                    "interrupted": True,
                }
            run = self._new_run(dims, measure, agg_func, len(dwd_data), fail_after)

        interrupted = self._consume_rows(run, dwd_data, dims, measure, agg_func, fail_after)
        if interrupted:
            run["status"] = RUN_INTERRUPTED
            run["updated_at"] = _now()
            store.save_dws_run(dws_table, run)
            entry["聚合状态"] = RUN_INTERRUPTED
            store.save_version(MODULE, entry_id, entry)
            done = len(run["processed_indexes"])
            return {
                "ok": False,
                "interrupted": True,
                "message": (
                    f"聚合在第 {done}/{len(dwd_data)} 行中断，已处理分组与异常明细已保留，"
                    "请调用续写接口完成剩余部分"
                ),
                "processed": done,
                "total": len(dwd_data),
            }

        return self._finish_run(entry, entry_id, dwd_table, dws_table, dims, measure, agg_func, run)

    def _new_run(self, dims: list[str], measure: str, agg: str, total: int,
                 fail_after: int | None) -> dict[str, Any]:
        return {
            "status": RUN_INTERRUPTED,
            "dims": dims,
            "measure": measure,
            "agg": agg,
            "total_rows": total,
            "processed_indexes": [],
            "groups": {},
            "abnormal_rows": [],
            "fail_after": fail_after,
            "started_at": _now(),
            "updated_at": _now(),
        }

    def _consume_rows(self, run: dict[str, Any], data: list[dict[str, Any]],
                      dims: list[str], measure: str, agg: str,
                      fail_after: int | None) -> bool:
        """逐行消费；返回 True 表示达到 fail_after 阈值而中断。"""
        need_measure = agg.upper() != "COUNT"
        done = set(run["processed_indexes"])
        budget = fail_after if fail_after is not None else len(data)
        processed_this_round = 0
        for index, row in enumerate(data):
            if index in done:
                continue
            empty_dims = [d for d in dims if d not in row or is_blank_dimension(row.get(d))]
            invalid_measure = need_measure and parse_measure(row.get(measure)) is None
            if empty_dims or invalid_measure:
                if empty_dims and invalid_measure:
                    reason = REASON_BOTH
                elif empty_dims:
                    reason = REASON_EMPTY_DIMENSION
                else:
                    reason = REASON_INVALID_MEASURE
                run["abnormal_rows"].append({
                    **dict(row),
                    "_row_no": index + 1,
                    "_window_key": "|".join(
                        "" if (d not in row or is_blank_dimension(row.get(d))) else str(row.get(d))
                        for d in dims),
                    "_empty_dims": ",".join(empty_dims),
                    "_invalid_measure": str(row.get(measure)) if invalid_measure else "",
                    "_reason": reason,
                })
            else:
                key = "|".join(str(row.get(d, "")) for d in dims)
                group = run["groups"].setdefault(
                    key, {d: row.get(d) for d in dims}, )
                group.setdefault("_values", [])
                group["_count"] = group.get("_count", 0) + 1
                if agg.upper() != "COUNT":
                    value = parse_measure(row.get(measure))
                    if value is not None:
                        group["_values"].append(value)
            run["processed_indexes"].append(index)
            done.add(index)
            processed_this_round += 1
            if processed_this_round >= budget:
                break
        return len(done) < len(data)

    def _finish_run(self, entry: dict[str, Any], entry_id: int, dwd_table: str,
                    dws_table: str, dims: list[str], measure: str, agg: str,
                    run: dict[str, Any]) -> dict[str, Any]:
        # 1. 由累加器还原合法聚合结果（含每分组明细总数）
        result: list[dict[str, Any]] = []
        for group in run["groups"].values():
            out = {d: group[d] for d in dims}
            values = group.get("_values", [])
            count = group.get("_count", 0)
            if agg.upper() == "SUM":
                out[f"{measure}_agg"] = sum(values)
            elif agg.upper() == "COUNT":
                out[f"{measure}_agg"] = count
            elif agg.upper() == "AVG":
                out[f"{measure}_agg"] = sum(values) / len(values) if values else 0
            elif agg.upper() == "MAX":
                out[f"{measure}_agg"] = max(values) if values else 0
            elif agg.upper() == "MIN":
                out[f"{measure}_agg"] = min(values) if values else 0
            else:
                out[f"{measure}_agg"] = sum(values)
            out["明细总数"] = count
            result.append(out)

        # 2. 合法主结果与异常分组分别落库，主表绝不包含异常行
        abnormal_rows = run["abnormal_rows"]
        store.set_table_rows(dws_table, result)
        store.set_table_rows(store.dws_abnormal_table(dws_table), abnormal_rows)

        # 3. 待修复窗口清单跨次保留：新窗口入清单，消失的窗口标记已修复
        windows = self._refresh_windows(dws_table, dims, abnormal_rows, entry.get("创建批次", ""))

        # 4. 结果版本号递增并记录运行态
        run["status"] = RUN_FINISHED
        run["updated_at"] = _now()
        run["result_version"] = int(entry.get("结果版本", 0)) + 1
        run["main_count"] = len(result)
        run["abnormal_count"] = len(abnormal_rows)
        run["window_count"] = len([w for w in windows if w["状态"] == WINDOW_PENDING])
        store.save_dws_run(dws_table, run)

        entry["结果版本"] = run["result_version"]
        entry["聚合状态"] = RUN_FINISHED
        entry["合法分组数"] = len(result)
        entry["异常行数"] = len(abnormal_rows)
        entry["异常窗口数"] = run["window_count"]
        entry["最近聚合时间"] = run["updated_at"]
        entry["abnormal"] = run["window_count"] > 0
        store.save_version(MODULE, entry_id, entry)

        # 5. 血缘（同一对上下游只注册一次，重复聚合不制造重复边）
        if not any(e.get("上游表名") == dwd_table and e.get("下游表名") == dws_table
                   for e in store.get_lineage_edges()):
            store.add_lineage(dwd_table, dws_table, ",".join(dims),
                              transform_logic=f"{agg}({measure}) GROUP BY {','.join(dims)}",
                              batch_id=entry.get("创建批次", ""))

        message = (
            f"汇总表 {dws_table} 已生成 v{run['result_version']}：合法分组 {len(result)} 个，"
            f"隔离异常记录 {len(abnormal_rows)} 条"
        )
        if run["window_count"]:
            message += f"，待修复窗口 {run['window_count']} 个（异常数据未混入主结果）"
        return {
            "ok": True,
            "message": message,
            "rows": result,
            "abnormal_rows": abnormal_rows,
            "windows": windows,
            "result_version": run["result_version"],
            "main_count": len(result),
            "abnormal_count": len(abnormal_rows),
        }

    def _refresh_windows(self, dws_table: str, dims: list[str],
                         abnormal_rows: list[dict[str, Any]], batch: str) -> list[dict[str, Any]]:
        previous = {w["window_key"]: w for w in store.get_dws_windows(dws_table)}
        # 本轮异常按窗口键归并
        current: dict[str, dict[str, Any]] = {}
        for row in abnormal_rows:
            key = row["_window_key"]
            bucket = current.setdefault(key, {
                "window_key": key,
                "维度值": {d: (row.get(d) if not is_blank_dimension(row.get(d)) else "") for d in dims},
                "原因集合": set(),
                "异常行数": 0,
                "样例行号": [],
            })
            bucket["原因集合"].add(row["_reason"])
            bucket["异常行数"] += 1
            bucket["样例行号"].append(row["_row_no"])

        now = _now()
        windows: list[dict[str, Any]] = []
        new_pending_keys: list[str] = []
        for key, bucket in current.items():
            old = previous.get(key)
            reasons = bucket["原因集合"]
            reason = REASON_BOTH if reasons == {REASON_EMPTY_DIMENSION, REASON_INVALID_MEASURE} \
                or REASON_BOTH in reasons else next(iter(reasons))
            if old and old.get("状态") == WINDOW_PENDING:
                record = dict(old)
                record["异常行数"] = bucket["异常行数"]
                record["最近发现"] = now
                record["样例行号"] = bucket["样例行号"]
                record["异常原因"] = reason
            else:
                record = {
                    "window_id": f"W{len(windows) + 1:03d}",
                    "window_key": key,
                    "维度值": bucket["维度值"],
                    "异常原因": reason,
                    "异常行数": bucket["异常行数"],
                    "状态": WINDOW_PENDING,
                    "首次发现": now,
                    "最近发现": now,
                    "修复时间": None,
                    "统计批次": batch,
                    "样例行号": bucket["样例行号"],
                }
                new_pending_keys.append(key)
            windows.append(record)

        # 本轮不再出现的历史窗口 -> 已修复，并核销残留通知
        fixed_keys = [key for key, old in previous.items()
                      if key not in current and old.get("状态") == WINDOW_PENDING]
        for key in fixed_keys:
            old = dict(previous[key])
            old["状态"] = WINDOW_FIXED
            old["修复时间"] = now
            windows.append(old)
            store.resolve_notifications("dws_window", f"{dws_table}:{key}")

        # 已无待修复窗口时，核销该数据集的发布阻断通知，避免通知残留
        if not current:
            store.resolve_dataset_notifications("publish_block", dws_table)

        # 新窗口发通知（去重：同表同键已有待处理通知则不重复发）
        open_notes = {(n.get("ref")) for n in
                      store.get_notifications(module=MODULE, only_open=True)}
        for key in new_pending_keys:
            ref = f"{dws_table}:{key}"
            if ref in open_notes:
                continue
            store.add_notification({
                "module": MODULE,
                "scope": "dws_window",
                "ref": ref,
                "level": "阻断",
                "title": f"DWS表 {dws_table} 出现待修复窗口",
                "message": f"窗口 {key or '（空维度）'} 存在异常记录，已隔离主结果，修复前阻断数据发布",
                "dataset": dws_table,
                "created_at": now,
            })

        store.save_dws_windows(dws_table, windows)
        return windows

    # ------------------------------------------------------------------
    # 结果查询：主结果 / 异常分组 / 窗口 / 运行态，均支持筛选排序翻页
    # ------------------------------------------------------------------

    def get_table_data(self, table_name: str) -> list[dict[str, Any]]:
        """主结果表只返回合法聚合分组，异常数据在独立隔离表中。"""
        return store.table_rows(table_name)

    def get_abnormal_data(self, table_name: str) -> list[dict[str, Any]]:
        return store.get_dws_abnormal_rows(table_name)

    def get_windows(self, entry_id: int) -> list[dict[str, Any]]:
        entry = self.get_entry(entry_id)
        if not entry:
            return []
        return store.get_dws_windows(entry.get("表名称", f"dws_{entry_id}"))

    def get_run(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        return store.get_dws_run(entry.get("表名称", f"dws_{entry_id}"))

    def query_rows(
        self,
        table_name: str,
        *,
        kind: str = "main",
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        sort_by: str | None = None,
        sort_order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
        """对DWS结果表（主结果或异常隔离表）做条件筛选、排序与翻页。

        返回 (当前页行, 过滤后总数, 可用列名)，分页定位与检索条件均可复核。
        """
        if kind == "abnormal":
            rows = store.get_dws_abnormal_rows(table_name)
        else:
            rows = [dict(r) for r in store.table_rows(table_name)]
        columns = sorted({key for row in rows for key in row.keys()})
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            kw = keyword.casefold()
            rows = [row for row in rows if any(kw in str(value).casefold() for value in row.values())]
        if sort_by and columns and sort_by in columns:
            def sort_key(row: dict[str, Any]) -> tuple[int, Any]:
                value = row.get(sort_by)
                try:
                    return (0, float(value))
                except (TypeError, ValueError):
                    return (1, str(value if value is not None else ""))
            rows = sorted(rows, key=sort_key, reverse=sort_order.lower() == "desc")
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total, columns
