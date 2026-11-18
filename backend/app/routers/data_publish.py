"""数据发布接口：维护发布版本，覆盖提交校验、确认发布、撤回版本等动作。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.data_publish import DataPublishService
from app.store import store

router = APIRouter(prefix="/api/data_publish", tags=["数据发布"])

service = DataPublishService()

LIST_FIELDS = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
STATUSES = ["草稿", "待校验", "已发布", "已撤回"]
RESERVED_QUERY = {"keyword", "status", "page", "size", "sort_by", "sort_order"}


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def table_query_filters(request: Request) -> dict[str, str]:
    """已发布结果的字段过滤：除保留参数外的任意列名均可作为过滤条件。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key not in RESERVED_QUERY and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按版本编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已发布、已撤回"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、版本编号关键字与状态过滤数据发布列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/notifications")
def list_notifications(only_open: bool = Query(default=True)) -> dict[str, Any]:
    """数据发布页面的跨模块通知（发布阻断等），窗口修复后随DWS通知一起核销。"""
    notes = store.get_notifications(module="data_publish", only_open=only_open)
    return {"items": notes, "total": len(notes)}


@router.get("/{entry_id}/blockers")
def get_blockers(entry_id: int) -> dict[str, Any]:
    """发布前复核：该版本目标数据集当前的待修复窗口清单。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 不存在或已归档")
    blockers = service.pending_windows(entry.get("数据集名称", ""))
    return {"items": blockers, "total": len(blockers), "blocked": len(blockers) > 0}


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
        headers={"Content-Disposition": 'attachment; filename="data_publish.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条发布版本明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条发布版本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="发布版本已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条发布版本执行提交校验、确认发布、撤回版本；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/data")
def get_published_data(
    entry_id: int,
    request: Request,
    keyword: str | None = Query(default=None, description="全列模糊检索"),
    sort_by: str | None = Query(default=None, description="排序字段"),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """返回发布版本关联的已发布数据（版本化快照），支持筛选、排序与翻页复核。"""
    items, total, columns, meta = service.query_published_rows(
        entry_id, filters=table_query_filters(request), keyword=keyword,
        sort_by=sort_by, sort_order=sort_order, page=page, size=size)
    if not meta and service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"发布版本 {entry_id} 不存在或已归档")
    return {"items": items, "total": total, "page": page, "size": size,
            "columns": columns, **meta}
