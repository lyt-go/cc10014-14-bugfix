"""安全聚合工具：按维度聚合DWD度量记录，显式隔离空维度与非法度量。

历史问题（本次修复）：旧实现把空维度和非法金额都静默当成 0 值继续聚合，
发布后的分组结果与真实数据对不上，错误零值被当成真实度量对外发布。

修复后的契约：
- 任一聚合维度为空（None、空串、纯空白）的记录 -> 异常分组，禁止进入主结果；
- SUM/AVG/MAX/MIN 的度量值无法解析为有限数值（含 NaN/Inf）-> 异常分组，
  绝不回退为 0；
- COUNT 只统计行数，不依赖度量可解析性，因此空度量不算非法；
- 合法记录与异常记录分别返回，由调用方分别落库。
"""
from __future__ import annotations

import math
from typing import Any

# 异常原因码，便于列表筛选与复核
REASON_EMPTY_DIMENSION = "空维度"
REASON_INVALID_MEASURE = "非法度量"
REASON_BOTH = "空维度+非法度量"


def is_blank_dimension(value: Any) -> bool:
    """维度值为空：None、缺失（调用方先判 in）、空串或纯空白都算空维度。"""
    if value is None:
        return True
    return str(value).strip() == ""


def parse_measure(value: Any) -> float | None:
    """把度量值解析为有限浮点数；无法解析或为 NaN/Inf 时返回 None，绝不回退零值。"""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 的子类，但 True/False 不是业务度量
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if text == "":
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(number):
        return None
    return number


def split_valid_and_abnormal(
    data: list[dict[str, Any]],
    dims: list[str],
    measure: str,
    agg: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 DWD 明细拆成「合法记录」与「异常记录」两个集合，分别落库使用。

    异常记录保留原始行内容，并附带窗口键、异常原因与涉及字段，方便复核。
    """
    need_measure = (agg or "SUM").upper() != "COUNT"
    valid_rows: list[dict[str, Any]] = []
    abnormal_rows: list[dict[str, Any]] = []
    for index, row in enumerate(data):
        empty_dims = [d for d in dims if d not in row or is_blank_dimension(row.get(d))]
        invalid_measure = need_measure and parse_measure(row.get(measure)) is None
        if not empty_dims and not invalid_measure:
            valid_rows.append(row)
            continue
        if empty_dims and invalid_measure:
            reason = REASON_BOTH
        elif empty_dims:
            reason = REASON_EMPTY_DIMENSION
        else:
            reason = REASON_INVALID_MEASURE
        abnormal_rows.append({
            **dict(row),
            "_row_no": index + 1,
            "_window_key": "|".join("" if is_blank_dimension(row.get(d)) else str(row.get(d)) for d in dims),
            "_empty_dims": ",".join(empty_dims),
            "_invalid_measure": str(row.get(measure)) if invalid_measure else "",
            "_reason": reason,
        })
    return valid_rows, abnormal_rows


def aggregate_valid_rows(
    data: list[dict[str, Any]],
    dims: list[str],
    measure: str,
    agg: str,
) -> list[dict[str, Any]]:
    """只对已经过隔离校验的合法记录做分组聚合。"""
    agg = (agg or "SUM").upper()
    groups: dict[str, dict[str, Any]] = {}
    for row in data:
        key = "|".join(str(row.get(d, "")) for d in dims)
        if key not in groups:
            groups[key] = {d: row.get(d) for d in dims}
            groups[key]["_values"] = []
            groups[key]["_count"] = 0
        groups[key]["_count"] += 1
        if agg != "COUNT":
            value = parse_measure(row.get(measure))
            # 数据已由 split_valid_and_abnormal 校验，这里仅做防御性跳过
            if value is not None:
                groups[key]["_values"].append(value)
    result: list[dict[str, Any]] = []
    for group in groups.values():
        out = {d: group[d] for d in dims}
        values = group["_values"]
        count = group["_count"]
        if agg == "SUM":
            out[f"{measure}_agg"] = sum(values)
        elif agg == "COUNT":
            out[f"{measure}_agg"] = count
        elif agg == "AVG":
            out[f"{measure}_agg"] = sum(values) / len(values) if values else 0
        elif agg == "MAX":
            out[f"{measure}_agg"] = max(values) if values else 0
        elif agg == "MIN":
            out[f"{measure}_agg"] = min(values) if values else 0
        else:
            out[f"{measure}_agg"] = sum(values)
        # 同时给出每个合法分组的明细总数，发布结果可复核
        out["明细总数"] = count
        result.append(out)
    return result


def safe_aggregate(
    data: list[dict[str, Any]],
    dims: list[str],
    measure: str,
    agg: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """安全聚合入口：返回 (合法聚合结果, 异常明细)，两者必须分别落库。"""
    valid_rows, abnormal_rows = split_valid_and_abnormal(data, dims, measure, agg)
    result = aggregate_valid_rows(valid_rows, dims, measure, agg)
    return result, abnormal_rows
