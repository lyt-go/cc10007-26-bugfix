"""数据漂移业务规则：状态流转、阈值告警与评分稳定性传播。

处理边界约束：
1. 状态机只允许 草稿 -> 规则评审中 -> 已启用 -> 已关闭 顺序前进一步；
2. 启用检测时从监控数据集重读漂移值并与阈值比较，超阈值立即进入告警中，
   不允许保留过期的「稳定/未告警」结论；
3. 关闭检测（检测中断）与关联评分卡稳定性重算在同一把锁内同批完成，
   评分卡得到「检测中断」结论，告警时点的特征偏移快照继续保留；
4. 全部联动写入走 coordinator，并发请求按幂等键去重。
"""
from __future__ import annotations

from typing import Any

from app.services.coordinator import (
    SOURCE_DRIFT_CLOSE,
    coordinator,
)
from app.store import store

MODULE = "datadrift"
REQUIRED_FIELDS = ["检测编号", "监控数据集", "基线版本"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已关闭"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用检测": "已启用", "关闭检测": "已关闭"}
NEGATIVE_ACTIONS = []


class DatadriftService:
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
            rows = [row for row in rows if keyword in str(row.get("检测编号", ""))]
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
        entry["漂移阈值数值"] = values.get("漂移阈值数值")
        entry["最近漂移值数值"] = values.get("最近漂移值数值")
        entry["阈值是否触发"] = False
        entry["告警状态"] = "未告警"
        rows.append(entry)
        return entry, []

    def available_actions(self, entry: dict[str, Any]) -> list[str]:
        try:
            idx = STATUS_ORDER.index(entry.get("status"))
        except ValueError:
            return []
        if idx >= len(STATUS_ORDER) - 1:
            return []
        target = STATUS_ORDER[idx + 1]
        return [action for action, state in ACTION_RULES.items() if state == target]

    def run_action(
        self, entry_id: int, action: str, *, idem_key: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        # 与联动写入共用同一把锁：告警状态与评分稳定性同批更新
        with coordinator.lock():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"漂移检测 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据漂移可执行范围"
            target = ACTION_RULES[action]
            current = entry.get("status")
            try:
                current_idx = STATUS_ORDER.index(current)
            except ValueError:
                return None, f"当前状态「{current}」不在允许的状态序列里"
            expected_idx = STATUS_ORDER.index(target)
            if expected_idx != current_idx + 1:
                return None, (
                    f"漂移检测当前为「{current}」，不能直接执行「{action}」；"
                    f"需先流转到「{STATUS_ORDER[current_idx + 1]}」"
                    if current_idx < len(STATUS_ORDER) - 1
                    else f"漂移检测已处于终态「{current}」，不可再执行「{action}」"
                )

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]

            # 同批传播：先落检测侧告警状态，再重算关联评分卡稳定性
            rechecks = coordinator.propagate_detection(
                entry,
                source=SOURCE_DRIFT_CLOSE if action == "关闭检测" else "检测流转复验",
                idem_key=idem_key,
            )
            card_count = len(coordinator.linked_scorecards(entry_id))
            message = f"漂移检测已{action}"
            if action == "关闭检测":
                message += f"，已同批复验 {card_count} 张关联评分卡并保留告警时点特征偏移"
            elif coordinator.is_threshold_breached(entry):
                message += "，监控数据集已触发漂移阈值，关联评分卡进入漂移告警"
            if rechecks:
                message += f"，生成复验明细 {len(rechecks)} 条"
            return entry, message

    def pending_drift_detail(self) -> dict[str, Any]:
        """数据漂移待处理明细：登记每张关联评分卡的复验明细数据集。"""
        rows = store.rows(MODULE)
        items: list[dict[str, Any]] = []
        for detection in rows:
            for card in coordinator.linked_scorecards(int(detection["id"])):
                items.append({
                    "检测编号": detection.get("检测编号"),
                    "检测状态": detection.get("status"),
                    "告警状态": detection.get("告警状态"),
                    "监控数据集": detection.get("监控数据集"),
                    "漂移阈值": coordinator.drift_threshold(detection),
                    "最近漂移值": coordinator.drift_value(detection),
                    "评分编号": card.get("评分编号"),
                    "评分卡状态": card.get("status"),
                    "稳定性结论": card.get("稳定性结论"),
                    "待复核": card.get("pending"),
                    "复验明细条数": len(card.get("复验明细", [])),
                })
        return {"total": len(items), "items": items}
