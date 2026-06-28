# 架构总览

> 给开发者建立心智模型的第一篇。读完应能回答："数据从哪来 → 经过几手 → 决策怎么形成 → 写到哪去"。

[← 回 Wiki 索引](README.md) · [下一章：4 角色委员会 →](02-agents.md)

---

## 一句话

**openInvest 是一个 4 层流水线**：外部触发器 → LLM 决策核 → 业务态机 → 落盘。
每层都可独立替换，互不知道对方实现细节。

```
┌─────────────────────────────────────────────────────────────┐
│  CONNECTORS（外部触发器，多消费者模式）                       │
│  web_api.py · skill/run.sh · desktop window                 │
└────────────────┬────────────────────────────────────────────┘
                 │  调用业务函数（不直接接 LLM）
                 ▼
┌─────────────────────────────────────────────────────────────┐
│  AGENTS（LLM 角色定义 + 提示词）                              │
│  macro · quant · risk · cio  ← 都受 core.committee 编排      │
└────────────────┬────────────────────────────────────────────┘
                 │  Coordinator-Worker 编排，信息分隔
                 ▼
┌─────────────────────────────────────────────────────────────┐
│  CORE（业务核心 + 并发安全 + Pydantic 校验）                  │
│  committee · portfolio_manager · memory_store · regime      │
└────────────────┬────────────────────────────────────────────┘
                 │  fcntl 锁 + atomic write
                 ▼
┌─────────────────────────────────────────────────────────────┐
│  MEMORY（frontmatter Markdown 持久化）                        │
│  portfolio.md · strategy.md · daily/<date>/<sym>.md         │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. Connectors 层（多消费者）

外部世界进入 invest 的入口。当前 connector **功能等价、各自负责自己的协议**：

| Connector | 触发协议 | 适合 |
|-----------|---------|------|
| `connectors/web_api.py` | HTTP REST + SSE | Web GUI / 程序化集成 |
| `skill/run.sh` (CLI) | Claude Code Skill | 让 Claude 自己当协调者跑 |
| `scripts/monitor_desktop_window.py` | 本地桌面窗口 | 盘中监控 / 手动记账 |

**关键约束**：connector 必须**只做协议转换**，业务逻辑全部 forward 给 `core/`。
违反这条 → connector 之间会出现行为飘移。

详见各 connector 子目录 README：
- [connectors/README.md](../../connectors/README.md)
- [skill/README.md](../../skill/README.md)

### 1.1 本地盘中监控窗口（2026-06-19 模块地图）

本地盘中监控不是 Web 预览页，主入口是独立桌面窗口：

| 层 | 入口/模块 | 职责 |
|----|-----------|------|
| 启动 | `start-invest-backend.bat` → `start_invest_backend.py` | 读取 `.env`，清理旧进程，启动监控、调度器和桌面窗口 |
| UI | `scripts/monitor_desktop_window.py` | 独立窗口主循环、卡片布局、悬浮窗协调 |
| UI 服务 | `scripts/monitor_window_services.py` | 读取监控快照、手动交易、关注列表、最新价 |
| 监控入口 | `jobs/market_monitor.py` | 仅保留 CLI 入口和兼容导出，旧导入仍可用 |
| 监控通用 | `jobs/market_monitor_common.py` | 路径、日志、交易时段常量、通用数值函数 |
| 行情/委员会 | `jobs/market_monitor_quotes.py` | 新浪行情、直接 Python 调用委员会，不依赖 8766 HTTP |
| 买卖点/止盈止损 | `jobs/market_monitor_entry_exit.py` | 连续触发状态、A 股持仓纪律止盈止损计划 |
| 风控护栏 | `jobs/market_monitor_guards.py` | 涨停买入拦截、重复交易冷却 |
| 告警优化 | `jobs/market_monitor_alerts.py` | 现金约束、仓位风险、LLM 审核修正后的最优提醒选择 |
| 快照/报告 | `jobs/market_monitor_snapshot.py` | 写 `data/market_monitor/latest_window.json` 和本地报告 |
| 运行编排 | `jobs/market_monitor_runtime.py` | 监控轮次、新闻摘要、账本同步、窗口快照刷新 |

盘中监控的数据流：

```
market_monitor_runtime.run_monitor_round()
  → load_config() + AccountLedger 读取真实账户 / 委员会影子账户
  → fetch_sina_prices() 拉最新价
  → call_committee() 直接调用 backend.server.run_committee_direct()
      ↳ 同一标的分别用 real 与 committee 账户上下文做独立评估
  → apply_repeated_trade_guard() / apply_limit_up_guard()
  → ledger.apply_committee_result() 只执行 shadow_result 到 committee 账户
  → select_optimal_actionable_alerts() 按 trading_mode 做现金/风险效用优化
  → update_entry_exit_alert_state()
  → build_monitor_window_snapshot()
  → scripts/monitor_desktop_window.py 监听 latest_window.json 并局部刷新 UI
```

桌面窗口的手动单标的分析是同一状态源的增量路径：双击卡片后，窗口直接调用最新委员会分析，成功结果会合成一行监控快照并回写 `data/market_monitor/latest_window.json`，随后主窗口重新排序和渲染。它不会修改真实持仓，只有用户点击“我已遵循买入/卖出”或手动交易面板执行时才写 `AccountLedger(real)`。

交易模式位于提醒优化层，不改委员会原始 verdict：

- `active_profit` / 主动盈利：默认模式，沿用胜率期望最大化和现金约束下的最优提醒选择。
- `cash_recovery` / 现金回收：提高买入阈值、保留更高现金垫，给释放现金的卖出更高效用；目标是尽量回收现金，同时避免无条件清仓。
- `risk_off` / 主动避险：提高买入门槛、压缩买入手数，并降低已持仓风险卖出的执行门槛；用于用户判断市场处于熊市或系统性风险偏高时。

桌面窗口现金栏的可用现金/T+2修正也走同一状态源：UI 调用 `scripts.monitor_window_services._correct_real_cash()`，内部写 `AccountLedger(real).correct_cash()`，ledger 再同步本地 config 和快照。

止盈止损和入场出场的边界要分清：

- `entry_exit_points` 是委员会/技术模型给出的入场、突破、回调、重新入场和技术出场参考，会随行情刷新。
- `position_exit_plan` 是已经持仓后的 A 股纪律计划，锚定真实成本和板块参数，盘中只检查触发，收盘后才允许追踪止损上移。
- A 股持仓纪律止损在 `evaluate_position_exit_plan_triggers()` 中判断，价格**跌破**锁定止损线才触发；止盈目标价达到即可触发。
- 每周 `jobs/weekly_exit_param_optimization.py` 会按板块回看最近数据，写入 `policy_quality_score`、`sell_win_rate_lower`、`profit_factor` 等质量字段；这些字段只用于校准已有持仓的减仓/卖出纪律，不参与买入点计算。
- `jobs.market_monitor_alerts.select_optimal_actionable_alerts()` 对已持仓纪律触发使用期望效用复核：止损是即时风险事件，止盈/减仓需连续确认；纪律卖出期望会被周度参数的 `sell_reliability`、Wilson 胜率下界和卖出后路径优势折减，再和委员会继续持有证据比较。委员会 `HOLD` 不能静默吞掉已确认纪律触发，但纪律参数也不会被当作 100% 可靠的硬卖点。
- 板块映射顺序是：东方财富浏览器请求头直连 → AkShare → `data/sector_cache.json` 本地缓存 → `market_monitor_config.json` 的 `sector`/`industry`；`data/` 被 git 忽略，移植时可手动带走缓存。盘中监控会读取同一份 `sector_cache` 覆盖运行时 A 股标的 `sector`，让委员会、窗口和止盈止损参数读取都对齐周更优化的东方财富板块。

### 1.2 真实账户单源与影子账户隔离

真实持仓以 `db/account_ledger.py:AccountLedger` 的 `real` 账户为单一可信源：

- 用户确认交易、手动交易面板、加入关注、取消关注、现金/T+2 修正都先写 ledger。
- 默认生产 ledger 每次更新 `real` 后立即同步 `jobs/market_monitor_config.json` 和 `data/market_monitor/latest_window.json`，兼容仍读取配置或快照的老路径。
- 自定义 DB 路径的测试/临时账本禁用外部同步，避免测试持仓覆盖真实配置。
- `sync_real_from_monitor_config()` 只刷新已有标的的 name/market/sector/industry/min_lot_size，并为 config 新标的做首次播种；不会覆盖已有真实股数、均价和现金。

影子账户隔离规则：

- `committee` 账户只由 `apply_committee_result()` 自动执行委员会建议，不读取或修改 `real` 持仓。
- `backend.server.run_committee_direct()` 可以在一次 symbol 调用中返回 `shadow_result`，供 `jobs.market_monitor_runtime` 写入影子账本。
- HTTP `/api/committee` 会清空 `shadow_result`；桌面窗口快照只使用真实账户结果，用户不会看到影子账户评估内容。
- 两套评估共享行情、新闻、宏观、基本面和技术指标，但组合摘要、现金、T+2、已有仓位和可买手数分别来自各自账户，不能互相影响优化器输出。

### 1.3 胜率数学参考

上游 2026-06-16 到 2026-06-23 changelog 对本项目最有价值的数学纪律：

- 公开命中率需要样本门槛：`n < 30` 时保留 hit/total 计数，但不展示具体 rate。
- 账本写入要幂等：重复执行同一外部成交或状态 patch 不应重复改变现金和持仓。
- 概率、回测和生产指标必须同源：forward return、行情窗口、FX/价格口径不能在训练、回测和生产之间漂移。
- 集中度与偿付能力不要多处自动兜底：风险 lens 应作为单一控制面，避免多个规则相互覆盖导致胜率归因失真。

当前落地位置：

- `scripts/export_accuracy.py` 在生成公开 `docs/accuracy_summary.json` 前对小样本 rate 置空。
- `core/position_exit_policy.py` / `jobs/weekly_exit_param_optimization.py` 对卖出胜率使用 Wilson 下界、样本可靠性收缩和风险调整期望效用。
- `db/account_ledger.py` 的 dual-account PnL 和 trades 是后续验证委员会胜率的真实对照基座。

---

## 2. Agents 层（LLM 角色）

四个独立 LLM session，**信息隔离**：

| Role | 看得到 | 看不到 | 输出 |
|------|--------|--------|------|
| Macro Strategist | VIX / TNX / USDCNY / 全球宏观 | 用户持仓、技术指标 | SIGNAL + STRENGTH + SCORE |
| Quant Analyst | 技术指标（RSI/MA/分位）+ REGIME 硬约束 | 用户持仓 | 看涨/看跌 + 信号强度 |
| Risk Officer | 持仓集中度 / 浮盈缓冲 / Dreaming insights | 技术指标 | 风险等级 + 集中度评分 |
| CIO | 三人完整 transcript | — | BUY/ACCUMULATE/HOLD/TRIM/SELL + confidence |

**Cross-Challenge 多轮辩论**（v3）：
- Round 1：Quant + Risk 独立陈述（并行）
- Round 2..N：互看对方上轮输出，调整自己（最多 4 轮）
- 收敛检测：SIGNAL + STRENGTH 两轮稳定 → 提前退出
- CIO 在 transcript 完整后综合出 verdict

详见 [02-agents.md](02-agents.md)。

---

## 3. Core 层（业务核心）

### 3.1 编排器：`core/committee.py`

```python
def run_committee(asset, market_data, macro_view, portfolio_summary,
                  *, max_debate_rounds=4, progress_callback=None):
    # Round 1: Quant + Risk parallel (信息隔离)
    # Round 2..N: cross-challenge with 收敛检测
    # CIO: 综合 transcript 出 verdict
```

`progress_callback` 让 Web SSE 端点可以实时 push stage 进度（"round_1_done" / "round_2_done" / "cio_done"）。

### 3.2 状态机：`core/portfolio_manager.py`

```python
with pm.with_portfolio_tx() as p:
    cash = dict(p.get("cash") or {})
    cash["CNY"] = cash.get("CNY", 0) + amount
    p["cash"] = cash
# with 退出时自动: validate → render body → atomic write
```

**所有写操作必须走这个 RMW 闭包**——保证 fcntl 文件锁 + Pydantic schema validate + 原子写。
异常时整个 tx 不落盘（commit-on-success）。

### 3.3 数据校验：`core/schemas.py`

Pydantic v2 强 schema，写入前必过：
- `cash` 必须是 `dict[str, NonNegativeFloat]`
- `holdings[].units >= 0`
- `holdings[].symbol` 必须非空
- 任何不满足 → ValidationError，写不进 memory

### 3.4 REGIME 硬约束：`core/regime.py`

5 阈值 + 5 类 regime，**确定性算法不走 LLM**，喂给 quant agent 当硬约束：

```
uptrend     → 禁 bearish 信号
downtrend   → 禁 bullish 信号
range_bound + price_quantile≤20%  → 偏 bullish（底部逢低）
range_bound + price_quantile≥80%  → 偏 bearish（顶部减仓）
crash       → 强制 neutral
```

LLM 不能违反 REGIME，CIO sanity check 会校验 verdict 与 REGIME 不冲突，冲突自动降级。

详见 [02-agents.md#regime-硬约束](02-agents.md#regime-硬约束)。

---

## 4. Memory 层（Markdown 持久化）

不用关系数据库，用 frontmatter Markdown：

```markdown
---
schema_version: 2
cash:
  CNY: 50000
  AUD: 1000
holdings:
  - symbol: NDQ.AX
    units: 50
    avg_cost: 38.50
    cost_currency: AUD
---

# 当前持仓
- CNY 现金: ¥50,000
- NDQ.AX: 50 股 @ A$38.50
```

**为什么这样**：
- frontmatter 给代码读写（atomic）
- body 给 LLM 直接看（不需要二次格式化）
- 同一份文件，Python 和 LLM 看到的永远一致
- git diff 友好，备份只需 cp -a

详见 [05-data-model.md](05-data-model.md) 和 [memory_layout.md](../memory_layout.md)。

---

## 5. 数据流（一次完整委员会）

以 `POST /api/committee/run` 为例：

```
1. web_api.committee_run()
     │
     ├─ 创建 task_id，写 .committee/<task_id>/status.json (queued)
     ├─ asyncio.create_task(_run_committee_task)
     └─ 立即返回 task_id 给前端
2. _run_committee_task (async, 后台跑)
     │
     ├─ 拉 strategy.target_assets → symbols
     ├─ run_macro_view(macro_data) ← 跨资产共享
     ├─ on_progress({phase: "macro_done"})
     │
     ├─ for sym in symbols (ThreadPoolExecutor 并行):
     │     │
     │     └─ run_committee_for_symbol(sym, ...)
     │           │
     │           ├─ get_history_data(sym, "2y")
     │           ├─ compute_metrics(df) → regime
     │           ├─ regime_brief = format_regime_brief(metrics)
     │           │
     │           ├─ Round 1: _parallel_ask([quant, risk])
     │           ├─ on_progress({phase: "round_1_done", round: 1, ...})
     │           │
     │           ├─ Round 2..N: cross_challenge with 收敛检测
     │           ├─ on_progress({phase: "round_N_done", ...})
     │           │
     │           ├─ CIO ask 综合 transcript
     │           ├─ on_progress({phase: "cio_done"})
     │           │
     │           └─ 落盘 daily/<date>/<sym>.md
     │
     └─ 写 .committee/<task_id>/status.json (done + result)

3. 前端订阅 SSE GET /api/committee/live/{task_id}
     │
     └─ 每个 on_progress 写 status.json 后 → SSE 流推 event
```

**关键点**：
- 前后端 1.5s 内拿到 task_id，不阻塞 HTTP
- 后台 ThreadPool 让多资产真并行（之前 v2 是串行）
- 每 stage 写一次 status.json，SSE 端点 watch 文件 mtime
- 25s keepalive 防 CF Access 5min idle 超时

---

## 6. 并发模型

四个并发场景：

| 场景 | 风险 | 缓解 |
|------|------|------|
| API/GUI 写入 + scheduler 扣款同时跑 | TOCTOU 丢更新 | `MemoryStore.transaction()` 单锁 RMW |
| 多线程 ThreadPool 同时调 LLM | （只读不冲突）| — |
| Web API 写 + CLI 写同时 | TOCTOU | 同上，fcntl 是进程级锁 |
| 多个 committee task 同时跑 | status.json 互踩 | 每 task_id 一个独立 dir |

并发压测（`tests/test_memory_store.py`）：
```
50 线程并发 cash["CNY"] += 1   → 最终 delta = 50.0  (0 lost updates)
20 轮 scheduler 扣款 + API 写入 race → delta 精确 = -37880  (0 lost updates)
```

详见 [05-data-model.md#并发安全](05-data-model.md#并发安全)。

---

## 7. 扩展点

每层都设计成可独立替换：

- **加 connector**（如 Telegram bot）：照 `web_api.py` 的边界新建一个文件，**不要碰 core**
- **加 agent 角色**（如 ESG 分析师）：在 `agents/` 加 prompt 文件，在 `committee.py:run_committee` 注册
- **换 LLM provider**（DeepSeek → OpenAI）：改 `agents/agent.py` 的 client init，prompt 不动
- **换持久化**（Markdown → SQLite）：实现 `MemoryStore` 同接口，core 不动

详见 [07-extending.md](07-extending.md)。

---

## 8. A股每日选股与双池架构

系统在每日收盘后支持执行 A股 候选股的自动化筛选（主要逻辑在 `scripts/daily_stock_selection.py` 与 `core/daily_stock_selector.py`）：

### 8.1 选股数据源与数据提取
1. **多源新闻过滤与 NLP 提取**：结合百度热搜与国内热点新闻，通过 `domestic_hot_news.py` 匹配预设的 20+ 个热门板块主题。利用正则表达式提取新闻正文中的 6 位 A股 代码，并通过常见公司简称映射表进行补充识别。
2. **北向资金流个股数据**：调用 `akshare` 获取东方财富的沪股通个股资金流数据，获取今日净买入额靠前的标的注入候选池。
3. **板块资金流数据**：获取主力资金净流入强、行业内排名靠前的板块。
4. **防反爬降级机制**：
   - 考虑到直连东方财富 API 容易因高频请求被反爬封锁 IP，系统引入了基于 Playwright Chromium 真实浏览器的爬虫机制（`utils/browser_scraper.py`）。
   - 当 `akshare` 或 `requests` 直连请求失败时，系统将自动激活 Playwright 拦截并模拟提取板块资金流、北向个股资金及百度热搜，保证数据源的高可用性。

### 8.2 双池架构 (Dual-Pool Architecture)
为了兼顾“大市值龙头防御”与“技术面异动实战”，选股结果从原有的单一列表拆分为**双池架构**：
* **板块龙头参考池 (Reference Pool)**：存放板块内的大市值稳定标的，条件为“板块龙头”或“基本面得分 `fundamental_score >= 60`”。
* **异动股实战池 (Action Pool)**：存放高波动、短线有明显技术面资金异动的标的，条件为“日内涨跌幅绝对值 `>= 5%`”或不属于参考池的其余候选。

在输出的选股 JSON payload 及 Android 客户端中，这两类标的会分别进行归类展示和跟踪。

---

## 下一步

→ [02-agents.md](02-agents.md) 看 4 个 LLM 角色具体在做什么 + 怎么 cross-challenge

→ [05-data-model.md](05-data-model.md) 看 v2 schema 演进 + 并发安全细节

→ [07-extending.md](07-extending.md) 看怎么加新功能
