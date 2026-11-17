"""端到端 HTTP 复验：通过真实 uvicorn 服务核验修复后的接口行为。

运行：先启动服务（端口 8011），再 .venv/bin/python -I tests/e2e_http.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
failures: list[str] = []


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        failures.append(name)


# 0. 启动对账已校正残留状态
_, cards = call("GET", "/api/scorecard?size=100")
_, drifts = call("GET", "/api/datadrift?size=100")
c1 = next(r for r in cards["items"] if r["评分编号"] == "SCOR-0001")
c3 = next(r for r in cards["items"] if r["评分编号"] == "SCOR-0003")
d3 = next(r for r in drifts["items"] if r["检测编号"] == "DATA-0003")
check("启动后 DATA-0003 不再残留未告警", d3["告警状态"] == "告警中" and d3["阈值是否触发"] is True)
check("启动后 SCOR-0003 不再显示稳定", c3["稳定性结论"] == "漂移告警")
check("SCOR-0001 仍为草稿", c1["status"] == "草稿" and c1["稳定性结论"] == "待检测")

# 1. 草稿直接启用被拦（HTTP 200 + ok=False，消息可读）
code, body = call("POST", "/api/scorecard/1/actions", {"values": {"action": "启用评分"}})
check("草稿启用返回 ok=False", code == 200 and body["ok"] is False and "不能直接执行" in body["message"])
_, cards = call("GET", "/api/scorecard?keyword=SCOR-0001")
check("拦截后 SCOR-0001 仍是草稿", cards["items"][0]["status"] == "草稿")

# 2. 正常顺序流转
call("POST", "/api/scorecard/1/actions", {"values": {"action": "提交评分校验"}})
code, body = call("POST", "/api/scorecard/1/actions", {"values": {"action": "启用评分"}})
check("校验后可启用", body["ok"] and body["entry"]["status"] == "已启用")

# 3. DATA-0002 启用（阈值 0.31>=0.25）-> 同批告警
code, body = call("POST", "/api/datadrift/2/actions", {"values": {"action": "启用检测", "idem_key": "e2e-enable-2"}})
check("DATA-0002 启用同批告警", body["entry"]["告警状态"] == "告警中", body["message"])
_, c2_detail = call("GET", "/api/scorecard/2")
check("SCOR-0002 同批进入漂移告警", c2_detail["稳定性结论"] == "漂移告警")

# 4. 关闭告警 -> 同批复验为检测中断，特征偏移保留
snapshot = c2_detail["告警时点特征偏移"]
code, body = call("POST", "/api/datadrift/2/actions", {"values": {"action": "关闭检测", "idem_key": "e2e-close-2"}})
check("关闭接口成功", body["ok"], body["message"])
_, c2_detail = call("GET", "/api/scorecard/2")
_, d2_detail = call("GET", "/api/datadrift/2")
check("关闭后 SCOR-0002 标记检测中断（非稳定）", c2_detail["稳定性结论"] == "检测中断")
check("DATA-0002 告警状态回写为告警已关闭", d2_detail["告警状态"] == "告警已关闭")
check("评分卡回写检测状态", c2_detail["关联检测状态回写"] == "已关闭")
check("告警时点特征偏移保留", c2_detail["告警时点特征偏移"] == snapshot)
check("历史首条复验结论仍是漂移告警", c2_detail["复验明细"][0]["稳定性结论"] == "漂移告警")

# 5. 并发/重复复验幂等：同键请求 3 次只生成一条
before = len(c2_detail["复验明细"])
for _ in range(3):
    call("POST", "/api/scorecard/2/recheck", {"values": {"idem_key": "e2e-recheck-x"}})
_, c2_detail = call("GET", "/api/scorecard/2")
check("同键重复复验只生成一条", len(c2_detail["复验明细"]) == before + 1)

# 6. 数据集与待处理明细接口
code, dataset = call("GET", "/api/scorecard/recheck-dataset")
check("复验明细数据集可导出", code == 200 and dataset["total"] >= 3 and dataset["基准检测"] == "DATA-0001")
code, pending = call("GET", "/api/datadrift/pending-detail")
check("漂移待处理明细登记评分卡复验", code == 200 and pending["total"] == 3)
code, overview = call("GET", "/api/scorecard/stability/overview")
check("稳定性汇总可核对", code == 200 and overview["total"] == 3)

# 7. /export 路由顺序正确（不再被 /{id} 拦截成 422）
code, exported = call("GET", "/api/scorecard/export")
check("评分卡 export 正常", code == 200 and exported["module"] == "scorecard")
code, exported = call("GET", "/api/datadrift/export")
check("数据漂移 export 正常", code == 200 and exported["module"] == "datadrift")

# 8. 对账接口重复执行不产生重复明细
code, body = call("POST", "/api/scorecard/reconcile", {})
check("对账接口可用", body["ok"] and body["entry"]["校正检测数"] == 0, str(body.get("entry")))

# 9. 终态检测拒绝再关闭
code, body = call("POST", "/api/datadrift/2/actions", {"values": {"action": "关闭检测"}})
check("终态拒绝重复关闭", body["ok"] is False)

print()
if failures:
    print(f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("全部 E2E 断言通过")
