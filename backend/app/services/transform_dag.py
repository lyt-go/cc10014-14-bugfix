"""转换编排 DAG 图服务：支持节点执行时消费上游表数据，写入下游表，并自动生成血缘。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "transform_dag"
REQUIRED_FIELDS = ["节点编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用节点": "已停用"}
NEGATIVE_ACTIONS = ["停用节点"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


class TransformDagService:
    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i + 1)),
                          "name": str(row.get("节点编号", "")),
                          "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id", "status", "pending", "abnormal")}})
        # 基于依赖节点构建真实边
        name_to_id = {n["name"]: n["id"] for n in nodes}
        for node in nodes:
            dep = str(node.get("依赖节点", ""))
            if dep and dep in name_to_id:
                edges.append({"from": name_to_id[dep], "to": node["id"], "label": "依赖"})
        # 补充基于输入输出表的血缘边
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                if a.get("输出表名") and a["输出表名"] == b.get("输入表名"):
                    edges.append({"from": a["id"], "to": b["id"], "label": "表依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for k in ["转换脚本", "执行顺序", "依赖节点", "失败策略", "调度周期"]:
            if k in values:
                entry[k] = values[k]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"转换节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于转换编排可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"转换节点已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]

    def execute_node(self, entry_id: int) -> dict[str, Any]:
        """执行转换节点：读取输入表数据，按转换脚本逻辑处理，写入输出表，并自动生成血缘和推导schema。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "转换节点不存在"}
        input_table = entry.get("输入表名", "")
        output_table = entry.get("输出表名", "")
        script = entry.get("转换脚本", "")
        # 读取输入数据
        input_data = store.table_rows(input_table)
        if not input_data:
            from app.services.dwd_layer import DwdLayerService
            dwd = DwdLayerService()
            # 尝试构建DWD数据
            dwd_entry = store.find_by_field("dwd_layer", "表名称", input_table)
            if dwd_entry:
                dwd.build_dwd_from_ods(dwd_entry["id"])
            input_data = store.table_rows(input_table)
        if not input_data:
            return {"ok": False, "message": f"输入表 {input_table} 无数据"}
        # 简单模拟SQL解析：GROUP BY 聚合
        output_data = self._execute_script(input_data, script, output_table)
        store.set_table_rows(output_table, output_data)
        # 自动生成血缘
        store.add_lineage(input_table, output_table,
                          field_map=entry.get("节点编号", ""),
                          transform_logic=script,
                          batch_id=entry.get("节点编号", ""))
        # 自动推导schema变更并注册
        from app.services.schema_registry import SchemaRegistryService
        sch = SchemaRegistryService()
        sch.derive_schema_from_lineage(output_table)
        return {"ok": True, "message": f"节点执行完成，输出{len(output_data)}行到 {output_table}", "rows": output_data}

    def _execute_script(self, data: list[dict[str, Any]], script: str,
                        output_table: str = "") -> list[dict[str, Any]]:
        if "GROUP BY" in script.upper():
            import re
            from app.services.aggregation import safe_aggregate
            # 找聚合字段：SUM/COUNT/AVG/MAX/MIN(...)
            agg_m = re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(([^)]+)\)", script, re.I)
            measure = agg_m.group(2).strip() if agg_m else ""
            agg = (agg_m.group(1).upper() if agg_m else "SUM")
            # 提取 GROUP BY 后面的维度字段（排除聚合函数部分），保留原始大小写
            after_gb = script.split("GROUP BY", 1)[1].strip() if "GROUP BY" in script.upper() else script
            # 去掉末尾的聚合函数部分
            agg_keywords = [" SUM(", " COUNT(", " AVG(", " MAX(", " MIN(",
                          " sum(", " count(", " avg(", " max(", " min("]
            for kw in agg_keywords:
                if kw in after_gb.upper():
                    idx = after_gb.upper().index(kw)
                    after_gb = after_gb[:idx]
                    break
            dims_raw = [d.strip() for d in after_gb.split(",")]
            dims = [d for d in dims_raw if d and not re.search(r'^(SUM|COUNT|AVG|MAX|MIN)\s*\(', d, re.I)]
            # 安全聚合：空维度与非法度量隔离，不再静默回退为0混入结果
            result, abnormal_rows = safe_aggregate(data, dims, measure, agg)
            # 异常分组独立落库（隔离表）；本轮无异常时同样覆写，避免上一轮残留
            store.set_table_rows(f"{output_table}__abnormal", abnormal_rows)
            return result
        # 默认透传
        return [dict(r) for r in data]
