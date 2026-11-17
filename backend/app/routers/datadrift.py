"""数据漂移接口：维护漂移检测，覆盖提交规则评审、启用检测、关闭检测与待处理明细。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.datadrift import DatadriftService

router = APIRouter(prefix="/api/datadrift", tags=["数据漂移"])

service = DatadriftService()

LIST_FIELDS = ["检测编号", "监控数据集", "基线版本", "漂移度量", "漂移阈值", "检测频率", "告警接收人", "最近漂移值"]
STATUSES = ["草稿", "规则评审中", "已启用", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按检测编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按检测编号与状态过滤数据漂移列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/pending-detail", response_model=dict)
def pending_detail() -> dict[str, Any]:
    """数据漂移待处理明细：登记关联评分卡的复验明细与待复核队列。"""
    return service.pending_drift_detail()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据漂移清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datadrift", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条漂移检测明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"漂移检测 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条漂移检测，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="漂移检测已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条漂移检测执行评审/启用/关闭；关闭与评分稳定性重算同批完成。

    业务校验失败返回 200 + ok=False（保持平台动作接口约定），message 带可读原因。
    """
    action = str(payload.values.get("action") or "").strip()
    idem_key = str(payload.values.get("idem_key") or "").strip() or None
    entry, message = service.run_action(entry_id, action, idem_key=idem_key)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
