"""汇总层管理接口：维护汇总表，覆盖提交校验、确认上线、标记下线等动作。

DWS聚合结果表相关接口：
- POST /{id}/aggregate        执行聚合（空维度/非法度量隔离，异常与正常分别落库）
- POST /{id}/aggregate/resume 中断后续写，只处理未完成部分
- GET  /{id}/data             主结果表（仅合法分组），支持筛选/排序/翻页
- GET  /{id}/abnormal         异常隔离表明细，支持筛选/排序/翻页
- GET  /{id}/windows          待修复窗口清单（含已修复历史）
- GET  /{id}/run              最近一次聚合运行态（中断定位与恢复依据）
- GET  /notifications         汇总层相关的跨模块通知
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.dws_layer import DwsLayerService
from app.store import store

router = APIRouter(prefix="/api/dws_layer", tags=["汇总层管理"])

service = DwsLayerService()

LIST_FIELDS = ["表编号", "统计粒度", "汇总维度", "度量字段", "聚合函数", "时间范围", "刷新周期", "创建批次"]
STATUSES = ["草稿", "待校验", "已上线", "已下线"]
# 结果表查询保留参数，其余 query 参数都按字段过滤处理
RESERVED_QUERY = {"keyword", "status", "page", "size", "kind", "sort_by", "sort_order", "resume", "fail_after"}


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def table_query_filters(request: Request) -> dict[str, str]:
    """结果表的字段过滤：除保留参数外的任意列名均可作为过滤条件。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key not in RESERVED_QUERY and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已上线、已下线"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、表编号关键字与状态过滤汇总层管理列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/notifications")
def list_notifications(
    only_open: bool = Query(default=True, description="默认只看待处理通知"),
) -> dict[str, Any]:
    """汇总层管理入口的跨模块通知（待修复窗口、发布阻断），修复后自动核销不残留。"""
    notes = store.get_notifications(module="dws_layer", only_open=only_open)
    return {"items": notes, "total": len(notes)}


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dws_layer.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条汇总表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇总表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条汇总表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="汇总表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条汇总表执行提交校验、确认上线、标记下线；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/aggregate")
def aggregate(
    entry_id: int,
    fail_after: int | None = Query(default=None, ge=1, description="演练用：处理N行后模拟中断"),
) -> dict[str, Any]:
    """从关联DWD表读取数据，按维度聚合生成DWS表；异常分组与正常聚合分别落库。"""
    result = service.aggregate_from_dwd(entry_id, resume=False, fail_after=fail_after)
    return result


@router.post("/{entry_id}/aggregate/resume")
def resume_aggregate(
    entry_id: int,
    fail_after: int | None = Query(default=None, ge=1, description="演练用：本轮再处理N行后中断"),
) -> dict[str, Any]:
    """Bugfix中断后从运行态定位并恢复：只续写尚未处理的下标，不重复计算已完成分组。"""
    result = service.aggregate_from_dwd(entry_id, resume=True, fail_after=fail_after)
    return result


@router.get("/{entry_id}/windows")
def get_windows(entry_id: int) -> dict[str, Any]:
    """待修复窗口清单（含已修复历史片段），供发布页与汇总层入口共同复核。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇总表 {entry_id} 不存在")
    windows = service.get_windows(entry_id)
    return {
        "items": windows,
        "total": len(windows),
        "pending": len([w for w in windows if w.get("状态") == "待修复"]),
        "fixed": len([w for w in windows if w.get("状态") == "已修复"]),
    }


@router.get("/{entry_id}/run")
def get_run(entry_id: int) -> dict[str, Any]:
    """最近一次聚合运行态：中断时给出已处理/总数，作为列表定位与恢复依据。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇总表 {entry_id} 不存在")
    run = service.get_run(entry_id)
    if run is None:
        return {"has_run": False}
    return {"has_run": True, "run": run}


@router.get("/{entry_id}/abnormal")
def get_abnormal_data(
    entry_id: int,
    request: Request,
    keyword: str | None = Query(default=None, description="全列模糊检索"),
    sort_by: str | None = Query(default=None, description="排序字段"),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """异常隔离表明细：空维度与非法度量记录，支持条件筛选、排序和翻页复核。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇总表 {entry_id} 不存在")
    table_name = entry.get("表名称", "")
    items, total, columns = service.query_rows(
        table_name, kind="abnormal", filters=table_query_filters(request), keyword=keyword,
        sort_by=sort_by, sort_order=sort_order, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size, "columns": columns}


@router.get("/{entry_id}/data")
def get_table_data(
    entry_id: int,
    request: Request,
    keyword: str | None = Query(default=None, description="全列模糊检索"),
    sort_by: str | None = Query(default=None, description="排序字段"),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """DWS主结果表（仅合法分组），支持按列筛选、排序、翻页，检索条件可复核。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇总表 {entry_id} 不存在")
    table_name = entry.get("表名称", "")
    items, total, columns = service.query_rows(
        table_name, kind="main", filters=table_query_filters(request), keyword=keyword,
        sort_by=sort_by, sort_order=sort_order, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size, "columns": columns}
