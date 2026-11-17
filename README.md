# 智能运营决策分析平台

面向用户行为序列分析、RFM分层、流失预警、智能推荐、归因建模、活动效果评估与实时运营决策的数据分析工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 行为序列 | `sequence` | 序列模式 | 模式编号、用户分群、事件序列 |
| RFM分层 | `rfm` | 分层规则 | 规则编号、最近消费天界、消费频次界 |
| 流失预警 | `churn` | 预警模型 | 模型编号、模型名称、特征变量 |
| 智能推荐 | `recommend` | 推荐策略 | 策略编号、推荐场景、召回算法 |
| 归因建模 | `attrmodel` | 归因模型 | 模型编号、模型名称、归因逻辑 |
| 活动评估 | `activity` | 活动方案 | 方案编号、活动名称、活动类型 |
| 实时决策 | `realtime` | 决策规则 | 规则编号、触发事件、决策条件 |
| 预算分配 | `budget` | 预算方案 | 方案编号、预算总额、渠道分配比例 |
| LTV估算 | `lifetime` | LTV模型 | 模型编号、模型名称、估算方法 |
| 实验变体 | `abvariant` | 变体方案 | 变体编号、关联实验、变体名称 |
| 运营看板 | `dashboard` | 看板视图 | 视图编号、视图名称、指标组合 |
| 评分卡 | `scorecard` | 评分卡 | 评分编号、评分对象、特征变量组 |
| 聚类分群 | `cluster` | 聚类任务 | 任务编号、聚类算法、特征维度 |
| 对比分析 | `comparison` | 对比组 | 对比编号、对比名称、实验组筛选 |
| 数据漂移 | `datadrift` | 漂移检测 | 检测编号、监控数据集、基线版本 |
| 推送计划 | `pushplan` | 推送方案 | 方案编号、推送内容、目标人群 |
| 指标归因 | `metricdrill` | 归因分析 | 分析编号、目标指标、波动方向 |
| 策略库 | `tacticlib` | 运营策略 | 策略编号、策略名称、策略类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 评分卡 × 数据漂移联动

两个模块通过 `backend/app/services/coordinator.py` 收敛跨模块写入：

- 状态机只允许沿 `草稿 -> 评审/校验中 -> 已启用 -> 已关闭/已废弃` 顺序前进一步，
  草稿态评分卡不能直接启用，跨级/逆向动作返回 `ok=false` 并附可读原因。
- 漂移检测启用/流转时从监控数据集重读漂移值与阈值（`最近漂移值数值`、
  `漂移阈值数值`），超阈值即告警；历史字符串字段全部保留，兼容旧核对口径。
- 漂移告警状态与评分卡稳定性结论在同一把 `RLock` 内同批更新；检测关闭（中断）
  后关联评分卡标记「检测中断」，不会保留过期的稳定结论。
- 告警关闭后按原监控数据集重算稳定性，「告警时点特征偏移」以快照永久保留，
  历史复验结论不被改写。
- 复验/动作支持 `idem_key` 幂等键，并发请求只生成一条可复核复验明细；
  评分卡当前稳定性结论始终唯一，不会同时保留稳定与告警两套结论。
- 服务启动时自动执行一次联动对账（`POST /api/scorecard/reconcile` 可手动再跑），
  仅校正确实不一致的记录，确定性幂等键保证重复执行不产生重复明细。
- 复验明细数据集：`GET /api/scorecard/recheck-dataset`；
  漂移待处理明细（登记评分卡复验）：`GET /api/datadrift/pending-detail`；
  DATA-0001 为复核基准。

## 测试

```bash
make test
# 或：cd backend && .venv/bin/python -I tests/test_scorecard_drift.py -v
# HTTP 端到端：先启动后端，再 .venv/bin/python -I tests/e2e_http.py
```

领域：数据工程
子方向：数据分析与数据应用
