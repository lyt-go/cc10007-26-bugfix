"""评分卡业务规则：状态流转、字段校验、稳定性复验与漂移联动。

状态流转约束（处理边界）：
草稿 -> 评分校验中 -> 已启用 -> 已废弃，只允许沿序列前进一步，
任何跨级/逆向动作都会被拦下，因此草稿态的 SCOR-0001 无法直接启用。
"""
from __future__ import annotations

from typing import Any

from app.services.coordinator import (
    DRIFT_ALERT,
    INTERRUPTED,
    SOURCE_MANUAL,
    coordinator,
)
from app.store import store

MODULE = "scorecard"
REQUIRED_FIELDS = ["评分编号", "评分对象", "特征变量组"]
STATUS_ORDER = ["草稿", "评分校验中", "已启用", "已废弃"]
ACTION_RULES = {"提交评分校验": "评分校验中", "启用评分": "已启用", "废弃评分": "已废弃"}
# 稳定性复验不是状态流转，单独走 coordinator，不进 ACTION_RULES
RECHECK_ACTION = "复验稳定性"
NEGATIVE_ACTIONS = []


class ScorecardService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("评分编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 新登记评分卡的稳定性口径默认待检测，避免无依据的稳定结论
        entry["关联检测id"] = values.get("关联检测id")
        entry["关联检测编号"] = values.get("关联检测编号")
        entry["稳定性结论"] = "待检测"
        entry["稳定性更新时间"] = None
        entry["告警时点特征偏移"] = None
        entry["复验明细"] = []
        rows.append(entry)
        return entry, []

    def available_actions(self, entry: dict[str, Any]) -> list[str]:
        """当前状态下真正可执行的流转动作（前端据此渲染，后端也会再拦一次）。"""
        try:
            idx = STATUS_ORDER.index(entry.get("status"))
        except ValueError:
            return []
        actions: list[str] = []
        if idx < len(STATUS_ORDER) - 1:
            target = STATUS_ORDER[idx + 1]
            actions.extend(
                action for action, state in ACTION_RULES.items() if state == target
            )
        actions.append(RECHECK_ACTION)
        return actions

    def run_action(
        self, entry_id: int, action: str, *, idem_key: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        with coordinator.lock():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"评分卡 {entry_id} 不存在或已归档"

            if action == RECHECK_ACTION:
                record, message = coordinator.recheck_scorecard(
                    entry, source=SOURCE_MANUAL, idem_key=idem_key
                )
                if record is None:
                    return None, message
                return entry, message

            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于评分卡可执行范围"
            target = ACTION_RULES[action]
            current = entry.get("status")
            # 状态机边界：只允许沿 STATUS_ORDER 前进一步
            try:
                current_idx = STATUS_ORDER.index(current)
            except ValueError:
                return None, f"当前状态「{current}」不在允许的状态序列里"
            expected_idx = STATUS_ORDER.index(target)
            if expected_idx != current_idx + 1:
                return None, (
                    f"评分卡当前为「{current}」，不能直接执行「{action}」；"
                    f"需先流转到「{STATUS_ORDER[current_idx + 1]}」"
                    if current_idx < len(STATUS_ORDER) - 1
                    else f"评分卡已处于终态「{current}」，不可再执行「{action}」"
                )
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"评分卡已{action}"

    def recheck(
        self, entry_id: int, *, idem_key: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """显式复验入口：按原监控数据集重新计算稳定性。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"评分卡 {entry_id} 不存在或已归档"
        record, message = coordinator.recheck_scorecard(entry, idem_key=idem_key)
        if record is None:
            return None, message
        return entry, message

    def stability_overview(self) -> dict[str, Any]:
        """评分稳定性汇总，供列表页与复验明细数据集核对。"""
        rows = store.rows(MODULE)
        return {
            "total": len(rows),
            "stable": sum(1 for row in rows if row.get("稳定性结论") == "稳定"),
            "drift_alert": sum(1 for row in rows if row.get("稳定性结论") == DRIFT_ALERT),
            "interrupted": sum(1 for row in rows if row.get("稳定性结论") == INTERRUPTED),
            "pending_check": sum(1 for row in rows if row.get("稳定性结论") == "待检测"),
            "recheck_records": sum(len(row.get("复验明细", [])) for row in rows),
        }
