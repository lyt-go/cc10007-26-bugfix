"""评分卡与数据漂移联动协调器。

所有跨模块写入都收敛在这里，保证：
1. 漂移告警状态与评分稳定性同批更新（同一把锁内完成，外部观察不到中间态）；
2. 检测中断后不会保留过期的稳定结论；
3. 并发复验按幂等键去重，同一评分卡同一批次只生成一条可复核明细；
4. 告警时点的特征偏移以快照形式保留，后续重算不覆盖历史结论。

历史数据集只带字符串占位字段（如「数据漂移样例2」），这里统一用
``_as_float`` 做兼容解析，解析不到时回落到种子数值字段，旧口径不受影响。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

SCORECARD_MODULE = "scorecard"
DATADRIFT_MODULE = "datadrift"

# 评分卡稳定性结论口径
STABLE = "稳定"
DRIFT_ALERT = "漂移告警"
INTERRUPTED = "检测中断"
PENDING_CHECK = "待检测"

# 漂移告警状态口径
ALERT_FIRING = "告警中"
ALERT_CLOSED = "告警已关闭"
ALERT_NONE = "未告警"

# 复验明细来源
SOURCE_DRIFT_CLOSE = "告警关闭复验"
SOURCE_MANUAL = "手动复验"
SOURCE_RECONCILE = "联动对账复验"


class DriftCoordinator:
    """跨模块联动的唯一入口，内部持有可重入锁保证同批原子写入。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---- 通用工具 -------------------------------------------------
    @staticmethod
    def now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _as_float(value: Any) -> float | None:
        """兼容历史字符串口径：能转数字就转，不能转返回 None。"""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def drift_value(self, detection: dict[str, Any]) -> float | None:
        numeric = self._as_float(detection.get("最近漂移值数值"))
        if numeric is not None:
            return numeric
        return self._as_float(detection.get("最近漂移值"))

    def drift_threshold(self, detection: dict[str, Any]) -> float | None:
        numeric = self._as_float(detection.get("漂移阈值数值"))
        if numeric is not None:
            return numeric
        return self._as_float(detection.get("漂移阈值"))

    def is_threshold_breached(self, detection: dict[str, Any]) -> bool:
        """按监控数据集当前读数与阈值计算是否触发漂移。"""
        value = self.drift_value(detection)
        threshold = self.drift_threshold(detection)
        if value is None or threshold is None:
            return False
        return value >= threshold

    # ---- 查询 -----------------------------------------------------
    def linked_scorecards(self, detection_id: int) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(SCORECARD_MODULE)
            if int(row.get("关联检测id", 0) or 0) == detection_id
        ]

    def linked_detection(self, card: dict[str, Any]) -> dict[str, Any] | None:
        detection_id = int(card.get("关联检测id", 0) or 0)
        if not detection_id:
            return None
        return store.find(DATADRIFT_MODULE, detection_id)

    # ---- 核心计算：从监控数据集重读并计算评分稳定性 ---------------
    def evaluate_stability(self, detection: dict[str, Any]) -> str:
        """沿监控数据集当前状态推导评分稳定性结论。

        - 检测已关闭（检测中断）且没有新一轮有效检测：不给稳定结论，标记检测中断；
        - 检测在启用/评审中且漂移值达到阈值：漂移告警；
        - 检测在启用/评审中且未达阈值：稳定；
        - 草稿或缺少数值口径：待检测。
        """
        status = detection.get("status")
        if status == "已关闭":
            return INTERRUPTED
        if status in ("已启用", "规则评审中"):
            return DRIFT_ALERT if self.is_threshold_breached(detection) else STABLE
        return PENDING_CHECK

    def evaluate_alert_state(self, detection: dict[str, Any]) -> str:
        status = detection.get("status")
        if status == "已关闭":
            return ALERT_CLOSED
        if status in ("已启用", "规则评审中"):
            return ALERT_FIRING if self.is_threshold_breached(detection) else ALERT_NONE
        return ALERT_NONE

    # ---- 同批传播 -------------------------------------------------
    def _write_detection_alert(self, detection: dict[str, Any]) -> None:
        """漂移告警状态与监控数据集读数同批落库。"""
        breached = self.is_threshold_breached(detection)
        detection["阈值是否触发"] = breached
        detection["告警状态"] = self.evaluate_alert_state(detection)
        detection["abnormal"] = breached and detection.get("status") != "已关闭"
        detection["pending"] = detection.get("status") != "已关闭"

    def _append_recheck(
        self,
        card: dict[str, Any],
        detection: dict[str, Any],
        *,
        source: str,
        idem_key: str,
        trigger_time: str | None = None,
    ) -> dict[str, Any]:
        """登记一条评分卡复验明细（幂等）。

        并发请求携带相同幂等键时，只返回首条明细，不会重复落库。
        """
        records: list[dict[str, Any]] = card.setdefault("复验明细", [])
        for existing in records:
            if existing.get("幂等键") == idem_key:
                # 并发请求命中同一幂等键：沿用首条明细，不重复落库
                return existing

        drift_value = self.drift_value(detection)
        threshold = self.drift_threshold(detection)
        stability = self.evaluate_stability(detection)
        # 保留告警时点的特征偏移：已经有快照就不覆盖，复验只追加不改写历史
        feature_shift = card.get("告警时点特征偏移")
        if feature_shift is None and self.is_threshold_breached(detection):
            feature_shift = self._build_feature_shift(detection)

        record = {
            "复验编号": f"RCHK-{card.get('评分编号')}-{len(records) + 1:03d}",
            "复验时间": self.now_text(),
            "来源": source,
            "幂等键": idem_key,
            "关联检测编号": detection.get("检测编号"),
            "监控数据集": detection.get("监控数据集"),
            "基线版本": detection.get("基线版本"),
            "复验时漂移值": drift_value,
            "复验时漂移阈值": threshold,
            "阈值是否触发": self.is_threshold_breached(detection),
            "告警时点特征偏移": feature_shift,
            "触发时间": trigger_time,
            "稳定性结论": stability,
            "处理方式": "新生成复验明细",
        }
        records.append(record)
        return record

    def _build_feature_shift(self, detection: dict[str, Any]) -> dict[str, Any]:
        """告警时点特征偏移快照，关闭告警后的重算不会改写它。"""
        return {
            "快照时间": self.now_text(),
            "监控数据集": detection.get("监控数据集"),
            "漂移度量": detection.get("漂移度量"),
            "告警时漂移值": detection.get("最近漂移值数值", detection.get("最近漂移值")),
            "漂移阈值": detection.get("漂移阈值数值", detection.get("漂移阈值")),
            "基线版本": detection.get("基线版本"),
        }

    def propagate_detection(
        self,
        detection: dict[str, Any],
        *,
        source: str,
        idem_key: str | None = None,
        trigger_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """把一条漂移检测的当前状态同批传播到全部关联评分卡。

        返回本次生成/命中的复验明细列表。锁内完成：
        检测告警状态、评分卡当前稳定性结论、复验明细三者同批更新。
        """
        with self._lock:
            self._write_detection_alert(detection)
            results: list[dict[str, Any]] = []
            for card in self.linked_scorecards(int(detection["id"])):
                stability = self.evaluate_stability(detection)
                # 当前结论唯一：评分卡不能同时保留稳定与告警两套当前结论
                card["稳定性结论"] = stability
                card["稳定性更新时间"] = self.now_text()
                card["abnormal"] = stability == DRIFT_ALERT
                card["pending"] = stability in (DRIFT_ALERT, INTERRUPTED)
                card["关联检测状态回写"] = detection.get("status")
                card["关联告警状态回写"] = detection.get("告警状态")
                if self.is_threshold_breached(detection) and card.get("告警时点特征偏移") is None:
                    card["告警时点特征偏移"] = self._build_feature_shift(detection)
                key = idem_key or f"{source}:{card['评分编号']}:{detection['id']}:{self.now_text()}"
                results.append(
                    self._append_recheck(
                        card, detection, source=source, idem_key=key, trigger_time=trigger_time
                    )
                )
            return results

    def reconcile_all(self) -> dict[str, Any]:
        """校正形成的复验明细数据集：从全部漂移检测重读并批量重算。

        仅当检测告警状态或关联评分卡当前结论与监控数据集读数不一致时才落
        复验明细；确定性幂等键保证服务重启或重复对账不会生成重复数据。
        """
        with self._lock:
            touched_cards: list[str] = []
            recheck_count = 0
            corrected_detections: list[str] = []
            batch_id = f"BATCH-{self.now_text()}"
            for detection in store.rows(DATADRIFT_MODULE):
                expected_alert = self.evaluate_alert_state(detection)
                expected_breached = self.is_threshold_breached(detection)
                cards = self.linked_scorecards(int(detection["id"]))
                needs_fix = (
                    detection.get("告警状态") != expected_alert
                    or detection.get("阈值是否触发") != expected_breached
                    or any(card.get("稳定性结论") != self.evaluate_stability(detection) for card in cards)
                )
                if not needs_fix:
                    continue
                corrected_detections.append(str(detection.get("检测编号")))
                # 确定性幂等键：同状态重复对账只命中首条明细
                idem_key = (
                    f"RECONCILE:{detection['id']}:{detection.get('status')}:"
                    f"{self.drift_value(detection)}:{self.drift_threshold(detection)}"
                )
                records = self.propagate_detection(
                    detection, source=SOURCE_RECONCILE, idem_key=idem_key
                )
                recheck_count += len(records)
                touched_cards.extend(card["评分编号"] for card in cards)
            return {
                "批次号": batch_id,
                "处理检测数": len(store.rows(DATADRIFT_MODULE)),
                "校正检测数": len(corrected_detections),
                "校正检测": corrected_detections,
                "覆盖评分卡": sorted(set(touched_cards)),
                "生成复验明细数": recheck_count,
            }

    def recheck_scorecard(
        self, card: dict[str, Any], *, source: str = SOURCE_MANUAL, idem_key: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """对单个评分卡按原监控数据集重新计算稳定性（复验）。

        并发请求在锁内按幂等键去重，只生成一条可复核明细。
        """
        with self._lock:
            detection = self.linked_detection(card)
            if detection is None:
                return None, "评分卡未关联监控数据集，无法复验稳定性"
            key = idem_key or f"{source}:{card['评分编号']}:{detection['id']}:{self.now_text()}"
            self.propagate_detection(detection, source=source, idem_key=key)
            record = next(
                (item for item in card.get("复验明细", []) if item.get("幂等键") == key), None
            )
            return record, f"评分卡 {card.get('评分编号')} 已按原监控数据集完成稳定性复验"

    def lock(self) -> threading.RLock:
        """供服务层状态流转复用同一把锁，避免与联动写入交叉。"""
        return self._lock


coordinator = DriftCoordinator()
