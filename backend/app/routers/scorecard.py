"""评分卡接口：维护评分卡，覆盖提交评分校验、启用评分、废弃评分、稳定性复验等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.coordinator import coordinator
from app.services.scorecard import RECHECK_ACTION, ScorecardService

router = APIRouter(prefix="/api/scorecard", tags=["评分卡"])

service = ScorecardService()

LIST_FIELDS = ["评分编号", "评分对象", "特征变量组", "变量分箱", "WOE编码", "变量权重", "基准分数", "评分刻度"]
STATUSES = ["草稿", "评分校验中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按评分编号检索"),
    status: str | None = Query(default=None, description="草稿、评分校验中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按评分编号与状态过滤评分卡列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stability/overview", response_model=dict)
def stability_overview() -> dict[str, Any]:
    """评分稳定性汇总：稳定/告警/中断/待检测各多少张，复验明细共多少条。"""
    return service.stability_overview()


@router.get("/recheck-dataset", response_model=dict)
def recheck_dataset() -> dict[str, Any]:
    """完整的校正形成的评分卡复验明细数据集（兼容历史核对口径，全量可复核）。"""
    rows = service.list_entries(page=1, size=10000)[0]
    records: list[dict[str, Any]] = []
    for card in rows:
        for record in card.get("复验明细", []):
            records.append({
                "评分编号": card.get("评分编号"),
                "评分卡状态": card.get("status"),
                "当前稳定性结论": card.get("稳定性结论"),
                **record,
            })
    return {
        "module": "scorecard",
        "total": len(records),
        "基准检测": "DATA-0001",
        "items": records,
    }


@router.post("/reconcile", response_model=ActionResult)
def reconcile() -> ActionResult:
    """从全部漂移检测重读监控数据集，批量重算评分稳定性，校正既有不一致。"""
    summary = coordinator.reconcile_all()
    return ActionResult(ok=True, message="联动对账完成，漂移告警与评分稳定性已同批校正", entry=summary)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出评分卡清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "scorecard", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条评分卡明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"评分卡 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条评分卡，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="评分卡已登记", entry=entry)


@router.post("/{entry_id}/recheck", response_model=ActionResult)
def recheck_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """按原监控数据集重新计算稳定性；携带相同幂等键的并发请求只生成一条明细。"""
    idem_key = None
    if payload is not None:
        idem_key = str(payload.values.get("idem_key") or "").strip() or None
    entry, message = service.recheck(entry_id, idem_key=idem_key)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条评分卡执行流转动作或复验；不允许的动作会被拦下并说明原因。

    业务校验失败返回 200 + ok=False（保持平台动作接口约定），message 带可读原因。
    """
    action = str(payload.values.get("action") or "").strip()
    idem_key = str(payload.values.get("idem_key") or "").strip() or None
    if action == RECHECK_ACTION:
        entry, message = service.recheck(entry_id, idem_key=idem_key)
    else:
        entry, message = service.run_action(entry_id, action, idem_key=idem_key)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
