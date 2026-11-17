"""评分卡 × 数据漂移联动修复的可复核验收测试。

覆盖核验场景：
1. 草稿启用拦截（SCOR-0001 草稿态不能直接启用）；
2. 阈值告警（DATA-0003 已超阈值，启动对账后不得再显示稳定）；
3. 关闭传播（DATA-0002 关闭后 SCOR-0002 重算为检测中断，特征偏移保留）；
4. 重复复验幂等（并发同键请求只生成一条可复核明细）；
5. 原告警时点的历史评分结果保持原状态不被改写；
6. DATA-0001 复核基准口径；
7. 评分卡当前结论唯一，不允许同时保留稳定与告警两套结论。

运行：cd backend && .venv/bin/python -I -m unittest tests.test_scorecard_drift -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.coordinator import (  # noqa: E402
    ALERT_CLOSED,
    ALERT_FIRING,
    DRIFT_ALERT,
    INTERRUPTED,
    PENDING_CHECK,
    STABLE,
    coordinator,
)
from app.services.datadrift import DatadriftService  # noqa: E402
from app.services.scorecard import RECHECK_ACTION, ScorecardService  # noqa: E402
from app.store import store  # noqa: E402


class ScorecardDriftTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例重置全局仓库单例；coordinator 与锁保持全局唯一，贴近真实运行
        store.reset()
        self.coordinator = coordinator
        self.sc = ScorecardService()
        self.dd = DatadriftService()

    def card(self, entry_id: int) -> dict:
        return store.find("scorecard", entry_id)

    def detection(self, entry_id: int) -> dict:
        return store.find("datadrift", entry_id)


class TestDraftEnableBlocked(ScorecardDriftTestBase):
    """场景一：复现草稿启用——必须被状态机边界拦下。"""

    def test_draft_cannot_enable_directly(self) -> None:
        self.assertEqual(self.card(1)["status"], "草稿")
        entry, message = self.sc.run_action(1, "启用评分")
        self.assertIsNone(entry)
        self.assertIn("不能直接执行", message)
        # 状态保持草稿，没有被错误推进
        self.assertEqual(self.card(1)["status"], "草稿")

    def test_draft_cannot_discard_directly(self) -> None:
        entry, _ = self.sc.run_action(1, "废弃评分")
        self.assertIsNone(entry)
        self.assertEqual(self.card(1)["status"], "草稿")

    def test_sequential_transition_allowed(self) -> None:
        self.sc.run_action(1, "提交评分校验")
        self.assertEqual(self.card(1)["status"], "评分校验中")
        self.sc.run_action(1, "启用评分")
        self.assertEqual(self.card(1)["status"], "已启用")
        self.sc.run_action(1, "废弃评分")
        self.assertEqual(self.card(1)["status"], "已废弃")

    def test_terminal_state_rejects_more_actions(self) -> None:
        # SCOR-0003 已启用，不能回退提交校验，只能继续废弃
        entry, _ = self.sc.run_action(3, "提交评分校验")
        self.assertIsNone(entry)
        self.assertEqual(self.card(3)["status"], "已启用")

    def test_available_actions_match_state_machine(self) -> None:
        self.assertEqual(
            self.sc.available_actions(self.card(1)),
            ["提交评分校验", RECHECK_ACTION],
        )
        self.assertEqual(self.sc.available_actions(self.card(3)), ["废弃评分", RECHECK_ACTION])


class TestThresholdAlert(ScorecardDriftTestBase):
    """场景二：阈值告警——超阈值的检测不能继续显示稳定。"""

    def test_seed_has_stale_stable_conclusion(self) -> None:
        # 修复前的错误口径：DATA-0003 读数 0.27 >= 阈值 0.2，却残留未告警
        d3 = self.detection(3)
        self.assertTrue(self.coordinator.is_threshold_breached(d3))
        self.assertEqual(d3["告警状态"], "未告警")
        self.assertEqual(self.card(3)["稳定性结论"], STABLE)

    def test_startup_reconcile_corrects_stale_state(self) -> None:
        summary = self.coordinator.reconcile_all()
        self.assertIn("DATA-0003", summary["校正检测"])
        d3, c3 = self.detection(3), self.card(3)
        self.assertTrue(d3["阈值是否触发"])
        self.assertEqual(d3["告警状态"], ALERT_FIRING)
        self.assertEqual(c3["稳定性结论"], DRIFT_ALERT)
        self.assertTrue(c3["abnormal"])
        self.assertEqual(c3["关联告警状态回写"], ALERT_FIRING)

    def test_reconcile_is_deterministic_no_duplicates(self) -> None:
        first = self.coordinator.reconcile_all()
        second = self.coordinator.reconcile_all()
        self.assertEqual(first["生成复验明细数"], 1)  # 只有 DATA-0003 需要校正
        self.assertEqual(second["校正检测数"], 0)
        self.assertEqual(second["生成复验明细数"], 0)
        # DATA-0001/DATA-0002 种子口径已正确，不产生噪声明细
        self.assertEqual(len(self.card(3)["复验明细"]), 1)

    def test_enable_detection_evaluates_alert_in_same_batch(self) -> None:
        # DATA-0002 评审中 -> 启用，阈值 0.31 >= 0.25，同批告警
        _, message = self.dd.run_action(2, "启用检测", idem_key="enable-2")
        self.assertEqual(self.detection(2)["告警状态"], ALERT_FIRING)
        self.assertEqual(self.card(2)["稳定性结论"], DRIFT_ALERT)
        self.assertIn("漂移阈值", message)


class TestClosePropagation(ScorecardDriftTestBase):
    """场景三：关闭传播——关闭告警后按原监控数据集重算并保留告警时点偏移。"""

    def setUp(self) -> None:
        super().setUp()
        self.snapshot_before = self.card(2)["告警时点特征偏移"]
        self.dd.run_action(2, "启用检测", idem_key="enable-2")

    def test_close_moves_card_to_interrupted_not_stable(self) -> None:
        _, message = self.dd.run_action(2, "关闭检测", idem_key="close-2")
        d2, c2 = self.detection(2), self.card(2)
        self.assertEqual(d2["status"], "已关闭")
        self.assertEqual(d2["告警状态"], ALERT_CLOSED)
        # 检测中断不能保留过期的稳定结论
        self.assertEqual(c2["稳定性结论"], INTERRUPTED)
        self.assertTrue(c2["pending"])
        self.assertIn("同批复验", message)

    def test_alert_time_feature_shift_preserved(self) -> None:
        self.dd.run_action(2, "关闭检测", idem_key="close-2")
        c2 = self.card(2)
        self.assertIsNotNone(c2["告警时点特征偏移"])
        self.assertEqual(c2["告警时点特征偏移"], self.snapshot_before)
        self.assertEqual(c2["告警时点特征偏移"]["告警时漂移值"], 0.31)

    def test_close_writes_back_to_drift_list_view(self) -> None:
        self.dd.run_action(2, "关闭检测", idem_key="close-2")
        c2 = self.card(2)
        self.assertEqual(c2["关联检测状态回写"], "已关闭")
        self.assertEqual(c2["关联告警状态回写"], ALERT_CLOSED)
        detail = self.dd.pending_drift_detail()
        row = next(item for item in detail["items"] if item["检测编号"] == "DATA-0002")
        self.assertEqual(row["稳定性结论"], INTERRUPTED)
        self.assertEqual(row["告警状态"], ALERT_CLOSED)
        self.assertTrue(row["待复核"])

    def test_recheck_after_close_uses_original_dataset(self) -> None:
        self.dd.run_action(2, "关闭检测", idem_key="close-2")
        entry, message = self.sc.recheck(2, idem_key="manual-after-close")
        self.assertIsNotNone(entry)
        latest = self.card(2)["复验明细"][-1]
        self.assertEqual(latest["监控数据集"], "数据漂移样例2")
        self.assertEqual(latest["稳定性结论"], INTERRUPTED)
        self.assertIn("原监控数据集", message)

    def test_historical_alert_record_keeps_original_state(self) -> None:
        # 原告警时点（种子明细）的评分结果保持原状态，不被后续重算改写
        original = self.card(2)["复验明细"][0]
        self.assertEqual(original["稳定性结论"], DRIFT_ALERT)
        self.dd.run_action(2, "关闭检测", idem_key="close-2")
        self.sc.recheck(2, idem_key="manual-1")
        original_after = self.card(2)["复验明细"][0]
        self.assertEqual(original_after["稳定性结论"], DRIFT_ALERT)
        self.assertEqual(original_after["复验时间"], "2026-11-10 09:00:00")


class TestConcurrentRecheck(ScorecardDriftTestBase):
    """场景四：并发请求到达后只生成可复核的复验明细数据集。"""

    def test_same_idempotency_key_single_record(self) -> None:
        barrier = threading.Barrier(8)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait()
                self.sc.recheck(3, idem_key="concurrent-key")
            except Exception as exc:  # pragma: no cover - 测试失败时暴露
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        records = self.card(3)["复验明细"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["幂等键"], "concurrent-key")

    def test_different_keys_each_get_record(self) -> None:
        for idx in range(5):
            self.sc.recheck(3, idem_key=f"key-{idx}")
        self.assertEqual(len(self.card(3)["复验明细"]), 5)

    def test_concurrent_close_and_recheck_single_current_conclusion(self) -> None:
        # 告警关闭回执与评分复验同时到达：当前结论始终唯一
        outcomes: list[str] = []
        lock = threading.Lock()

        def close_worker() -> None:
            self.dd.run_action(2, "启用检测", idem_key="enable-2")
            entry, _ = self.dd.run_action(2, "关闭检测", idem_key="close-2")
            with lock:
                outcomes.append(("close", entry["status"] if entry else "failed"))

        def recheck_worker() -> None:
            entry, _ = self.sc.recheck(2, idem_key="recheck-race")
            with lock:
                outcomes.append(("recheck", entry["稳定性结论"] if entry else "failed"))

        t1 = threading.Thread(target=close_worker)
        t2 = threading.Thread(target=recheck_worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        c2 = self.card(2)
        # 无论谁先到达，评分卡只有一个当前结论字段，且取值合法
        self.assertIn(c2["稳定性结论"], {DRIFT_ALERT, INTERRUPTED})
        self.assertNotEqual(c2["稳定性结论"], STABLE)
        self.assertEqual(len(outcomes), 2)


class TestBaselineAndDatasets(ScorecardDriftTestBase):
    """DATA-0001 复核基准、复验明细数据集与历史口径兼容。"""

    def test_data_0001_baseline_not_alert(self) -> None:
        d1 = self.detection(1)
        self.assertFalse(self.coordinator.is_threshold_breached(d1))  # 0.08 < 0.25
        self.coordinator.reconcile_all()
        c1 = self.card(1)
        # 草稿态检测不给稳定结论
        self.assertEqual(c1["稳定性结论"], PENDING_CHECK)
        self.assertEqual(c1["关联检测编号"], "DATA-0001")

    def test_recheck_dataset_export_shape(self) -> None:
        self.coordinator.reconcile_all()
        self.dd.run_action(2, "启用检测", idem_key="enable-2")
        self.dd.run_action(2, "关闭检测", idem_key="close-2")
        rows = store.rows("scorecard")
        flat: list[dict] = []
        for card in rows:
            for record in card.get("复验明细", []):
                flat.append({"评分编号": card["评分编号"], **record})
        self.assertTrue(flat)
        required = {
            "评分编号", "复验编号", "复验时间", "来源", "幂等键",
            "监控数据集", "复验时漂移值", "复验时漂移阈值", "稳定性结论",
        }
        for record in flat:
            self.assertTrue(required.issubset(record.keys()))

    def test_legacy_string_fields_remain(self) -> None:
        # 历史核对口径：字符串占位字段仍然保留，数值字段是新增口径
        d2 = self.detection(2)
        self.assertEqual(d2["漂移阈值"], "数据漂移样例2")
        self.assertEqual(d2["漂移阈值数值"], 0.25)
        c2 = self.card(2)
        self.assertEqual(c2["WOE编码"], "SCOR-0002")
        self.assertEqual(c2["评分编号"], "SCOR-0002")

    def test_no_card_holds_two_current_conclusions(self) -> None:
        self.coordinator.reconcile_all()
        for card in store.rows("scorecard"):
            self.assertIn(card["稳定性结论"], {STABLE, DRIFT_ALERT, INTERRUPTED, PENDING_CHECK})
            self.assertIsInstance(card["稳定性结论"], str)

    def test_stability_overview_counts(self) -> None:
        self.coordinator.reconcile_all()
        overview = self.sc.stability_overview()
        self.assertEqual(overview["total"], 3)
        self.assertEqual(overview["drift_alert"], 2)  # SCOR-0002、SCOR-0003
        self.assertEqual(overview["pending_check"], 1)  # SCOR-0001 基准草稿


if __name__ == "__main__":
    unittest.main(verbosity=2)
