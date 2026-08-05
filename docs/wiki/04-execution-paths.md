# 双执行路径

> 同一套委员会逻辑，**两个独立实现**：Coordinator 路径让用户的 Claude 当协调者真 spawn 4 个 subagent，
> Direct 路径用 DeepSeek + ThreadPool 同进程多线程。结果可能不同——这是 feature。

[← 03-dreaming](03-dreaming.md) · [Wiki 索引](README.md) · [05-data-model →](05-data-model.md)

> **2026-05 重命名**：旧文档叫 "Skill 路径" vs "Web/Cron 路径"。但 skill 现在
> 同时支持两条（`prepare_committee` + spawn subagent 走 Coordinator；
> `run_committee` 走 Direct，任意非 Claude agent 也能用），所以统一改叫
> **Coordinator** vs **Direct**。Direct 包含原 cron 路径 + Web GUI 触发版本 +
> skill 里的 `run_committee` 子命令。

---

## TL;DR

| 路径 | 谁能用 | 触发 | 协调者 | Worker 实现 | 模型 | 真 subagent? | 成本 |
|------|--------|------|--------|-------------|------|-------------|------|
| **Coordinator** | 仅 Claude Code（要 `Agent({...})` 工具）| skill `prepare_committee SYM` | 用户的 Claude | `Agent({subagent_type})` 真 spawn 4 subagent（subprocess 隔离）| Claude 4 | ✅ | 由用户订阅承担（项目 ¥0）|
| **Direct** | 任意 agent（Cursor / Cline / Codex / 普通脚本）+ cron | skill `run_committee SYM` / `POST /api/committee/run` / cron `daily_report` | `core/committee.py` | 4 个 `SDKAgent` + `ThreadPoolExecutor` 同进程多线程 | DeepSeek-Chat | ❌（信息分隔但非 subprocess）| ¥0.01-0.03 一次 |

**功能等价**：同一套 prompt，同一套 cross-challenge 协议，同一套 REGIME 硬约束。
**模型不同**：verdict 可能不同——这是**对比验证机制**而不是 bug。

---

## 1. Coordinator 路径（仅 Claude Code）

### 触发

```bash
~/.claude/skills/invest/run.sh prepare_committee NDQ.AX
```

或在 Claude Code 对话里说："帮我跑委员会分析 NDQ"——Claude 会自动调用 skill。

### 实现

`skill/run.sh prepare_committee` 做的事：

1. 拉 NDQ.AX 行情 + REGIME 算好
2. 把 4 个角色的 system prompt + 输入数据写到 `/tmp/.committee/<task_id>/{macro,quant,risk,cio}.md`
3. 输出一段 instruction 给用户的 Claude，**告诉它去 spawn 4 个 subagent 跑**

用户的 Claude 看到 instruction 后：

```
Agent({
  subagent_type: "general-purpose",
  description: "Macro analysis for NDQ.AX",
  prompt: <macro 角色的 system prompt + 输入>
})
Agent({...})  // quant
Agent({...})  // risk

// 等三个 subagent 完成后
Agent({...})  // cio 综合 transcript
```

**真隔离**：每个 Agent() 都是 subprocess，自带独立 context window。Claude 4 当 worker，能力上限高。

### 优势

- ✅ 项目本身**零 API 成本**（用户已订阅 Claude，跑 100 次也 0 元）
- ✅ Claude 模型能力强，verdict 质量通常高
- ✅ 真 subagent 隔离 = 真 Agent Teams（subprocess，不是同进程绑 token）

### 限制

- ❌ 必须在 Claude Code 里跑，不能后台 cron
- ❌ 用户得在场触发
- ❌ 没有 SSE 直播，结果是一次性输出
- ❌ Skill 跑完后状态不持久化到 invest memory（除非用户手动 fwd）

详见 [skills/invest/SKILL.md](../../skills/invest/SKILL.md)。

---

## 2. Direct 路径（任意 agent / cron / Web GUI）

三个触发入口都走同一份 `core/committee.py:run_committee`：

| 入口 | 谁用 | 备注 |
|------|------|------|
| `~/.claude/skills/invest/run.sh run_committee SYM` | Cursor / Cline / Codex / 普通脚本 / DeepSeek 本地 / 任意 agent | 一条命令拿 verdict JSON + CIO memo |
| `POST /api/committee/run` | Web GUI 的"触发/直播"按钮 | 异步 + SSE 进度推送 |
| cron `0 3 * * *` 跑 `jobs/daily_report.py` | 服务器自动每日 | 跑全部 target_assets，可选发邮件 |
| `python -m jobs.market_monitor` | 本地盘中桌面窗口/启动脚本 | 交易时段按委员会刷新快照，不要求 8766 HTTP 可用 |

### 触发

**手动**：`POST /api/committee/run` （Web GUI 「触发/直播」按钮）
**自动**：cron `0 3 * * *` 跑 `jobs/daily_report.py`

### 实现

`core/committee.py:run_committee` 在同一进程内：

```python
# Round 1: Quant + Risk 并行
with ThreadPoolExecutor(max_workers=2) as ex:
    quant_future = ex.submit(_ask, quant_agent, quant_prompt)
    risk_future = ex.submit(_ask, risk_agent, risk_prompt)
    quant_r1 = quant_future.result()
    risk_r1 = risk_future.result()

# Round 2..N: cross-challenge with 收敛检测
for round_idx in range(2, max_debate_rounds + 1):
    quant_rN, risk_rN = _parallel_ask([
        (quant_agent, quant_prompt_rN),
        (risk_agent, risk_prompt_rN),
    ])
    if _check_convergence(...):
        break

# CIO 串行
cio_memo = _ask(cio_agent, cio_prompt_with_full_transcript)
```

每个 `_ask` 调一次 DeepSeek-Chat HTTP API。同一进程多线程并发。

**信息隔离靠规约**：每个 agent 拿到自己的 prompt 字符串，不共享变量。但物理上是同进程同 GIL，不是 subprocess。

### 优势

- ✅ 后台 cron 自动跑（每天 03:00 daily_report）
- ✅ Web GUI SSE 实时直播 stage 进度
- ✅ 多资产真并行（ThreadPoolExecutor 包多个 `run_committee_for_symbol`）
- ✅ 状态持久化（`.committee/<task_id>/status.json` + `daily/<date>/<sym>.md`）
- ✅ DeepSeek-Chat 响应快（2-3s/次 vs Claude 5-10s/次），整体收敛 ~16s

### 限制

- ❌ DeepSeek 模型能力 < Claude 4，复杂场景 verdict 质量略低
- ❌ 用户付 DeepSeek 费用（虽然便宜，¥0.01-0.03 一次）
- ❌ 同进程不是真 subagent（虽然信息隔离正确，但 marketing 上 Claude 党会挑刺）

---

## 2.1 本地盘中监控 Direct 路径

盘中监控走 Direct 路径，但它不是 `POST /api/committee/run` 的浏览器预览分支。当前推荐链路是：

```
start-invest-backend.bat
  → start_invest_backend.py
  → python -m jobs.market_monitor
  → scripts/monitor_desktop_window.py
```

核心原则：

- `jobs.market_monitor_quotes.call_committee()` 直接调用 `backend.server.run_committee_direct()`，不通过 `http://127.0.0.1:8766`。
- `jobs.market_monitor_quotes.build_behavioral_factor_context()` 在每轮委员会前只构建一次 A 股横截面。它读取两年日线，调用 `core.ashare_behavioral_factor.assess_behavioral_universe()`，并把每只标的评估作为可选 `behavioral_factor` 传给 Direct 委员会。
- 因子前四成员与逆波动目标仓位存放在 `data/behavioral_factor_state.json`，每 5 个交易日更新。分数和最近三个月标的级证据可随新收盘数据重算，但五日内不切换目标成员；`data/` 被 git 忽略。
- A 股优化器优先使用行为因子的经验 20 日收益和目标仓位。标的级优化器权重由最近 63 个交易日的因子方向超额收益决定，范围 `0.55~1.0`；港股等非 A 股保持原 `_estimate_expected_return_pct()` 路径。
- 行为因子的 5 个百分点免交易带只抑制因子目标附近的反复调仓。委员会/优化器给出的高置信卖出仍可越过免交易带；成本锚定持仓纪律已停用，不再作为越过免交易带的依据。
- 每个标的一次 Direct 调用可以同时携带真实账户和影子账户上下文：`position_pct/cash/holdings` 属于 `real`，`shadow_position_pct/shadow_cash/shadow_holdings` 属于 `committee`。
- 返回给主窗口、报告和 HTTP `/api/committee` 的是真实账户评估；`shadow_result` 只在 Python 内部给 `jobs.market_monitor_runtime` 执行影子账户，不展示给用户。
- `jobs.market_monitor_runtime.run_monitor_round()` 是一轮盘中监控的唯一编排入口，负责行情、委员会、账本、新闻、告警和快照输出。
- 每次 Direct 分析生成共享 `analysis_id`，再为 real/committee 派生不同 `decision_id`；真实结果和影子结果分别应用本账户的重复交易历史，不能用影子成交压掉真实账户建议。
- `jobs.market_price_sentinel.IntradayPriceSentinel` 在行情拉取后记录新鲜 10 分钟样本；按标的优先、板块回退并按交易时段估计 99.5% 双尾残差阈值。样本不足 200 或行情陈旧时明确 unavailable；触发只把上下文注入当前 Direct 委员会并写快照，不调用旧 HTTP 端口、不自动成交。
- `scripts/monitor_desktop_window.py` 只消费 `data/market_monitor/latest_window.json`，窗口跟随委员会/选股/新闻输出文件变化刷新，不再单独定义业务刷新频率。
- 桌面和 Android 主界面的行为因子目标前四同样只消费快照行的可选 `behavioral_factor`：筛选 `selected=true`、按 `target_weight_pct` 降序取四只。该展示不触发因子重算、不写账本，也不把模型目标成员解释成真实持仓；旧快照缺少字段时仅隐藏该区域。
- 周末新闻龙头点击复用现有单标的委员会入口：桌面由 `MonitorNewsMixin._open_news_leader_analysis()` 关闭新闻悬浮层并调用 `_open_analysis_dialog()`；Android `NewsDialog` 直接回调 `MainScreen` 的 `activeCommitteeSymbol/activeCommitteeRow`。不在监控快照中的标的使用 `_is_resolving` 进度状态补全代码与行情，接口结构保持不变。
- 周末摘要保存 `_source_date_range`，桌面来源栏同时显示“新闻日期 · summary/report 文件名”；测试必须把 `CACHE_DIR` 重定向到 `tmp_path`，禁止用测试样本覆盖真实周末摘要。
- 桌面窗口通过 `scripts/monitor_window_tray.py` 接入系统托盘，图标读取 Android APK 的 `mipmap-xxxhdpi/ic_launcher.webp` 并按密度降级。标题栏关闭按钮和系统关闭事件只隐藏窗口；单击托盘默认菜单项恢复并临时置顶，右键“退出 OpenInvest”才进入 `_on_close()`，停止托盘与后台服务。托盘回调只写线程安全队列，所有 Tk 操作仍在主线程执行。
- 桌面端远程下载配置时，普通 JSON/.env 先走备份原子写；替换 `db/accounts.db` 前调用 `_stop_background_services()` 停止同项目的 `backend.server`、`uvicorn` 和监控任务以释放 Windows 文件锁，账本校验恢复后调用 `AccountLedger.sync_to_config_and_snapshot()`。只有同步前后端端口确实可用时才通过统一启动器静默恢复后端。
- 桌面窗口里双击单标的会走 `scripts.monitor_window_services._run_latest_committee_for_row()` 直接跑最新委员会；成功后由 `scripts.monitor_window_analysis._dialog_row_from_committee_result()` 合成行状态，再通过 `scripts.monitor_desktop_window.MonitorWindow._apply_committee_row_update()` 回写内存行和 `latest_window.json`。
- 当窗口退回配置兜底快照时，`scripts.monitor_window_services._config_stock_row()` 的技术指标只读缓存：先取最近委员会结果里的 `technical/market_metrics/metrics/entry_exit_points`，缺失时再由 `_compute_tech_from_local_history()` 只读 `db/market_data.db` 计算 `ma20/ma120/atr_pct` 并生成 `regime_brief`。该路径不调用 `utils.market_data_provider.get_history_data()` 或旧历史行情 provider，避免 UI 刷新触发联网、行情同步或 DB 写入。
- Web/API 的 `POST/PUT/DELETE /api/holdings` 会在账本更新后追加后台任务：新增标的先预拉 2 年历史行情写入缓存，再刷新 `latest_window.json`；更新/删除则直接刷新快照。这是配置变化后的快照维护，不是桌面 UI 刷新路径。
- 弹框默认关闭，稳定窗口是主交互面；只有显式开启相关环境变量时才恢复旧 Windows 弹框。

模块定位：

| 文件 | 查什么问题 |
|------|------------|
| `jobs/market_monitor.py` | CLI 入口、旧导入兼容 |
| `jobs/market_monitor_runtime.py` | 一轮监控为什么跑、什么时候跑、快照何时写 |
| `jobs/market_monitor_quotes.py` | 行情和委员会直接调用 |
| `jobs/market_monitor_entry_exit.py` | 买卖点连续触发；旧持仓止盈止损函数仅保留兼容 |
| `jobs/market_monitor_alerts.py` | 为什么某只标的排在前面、为什么被现金/风险约束压掉 |
| `jobs/market_monitor_guards.py` | 涨停和重复交易为什么被拦截 |
| `jobs/market_monitor_snapshot.py` | 主窗口看到的字段从哪里来 |
| `jobs/market_price_sentinel.py` | 10 分钟经验尾部为何触发/为何不可用、方向冷却 |
| `scripts/monitor_window_*.py` | 主窗口 UI、悬浮详情、手动交易、选股/新闻卡片和配置兜底快照 |

对新维护者最容易混淆的一点：

- 入场/出场点来自 `entry_exit_points`，服务于“现在能不能买/加/减”的技术确认。
- 成本锚定的 `position_exit_plan`、`position_exit_policy` 和 `position_exit_discipline_review` 已从生产路径删除；同名空字段/空函数仅用于旧接口兼容，周度参数模块与调度不再存在。
- 卖出提醒阈值在 `jobs/market_monitor_alerts.py`：当前只看委员会/优化器 `SELL/TRIM`、负向建议金额、可卖手数、交易模式、A 股 T+1 和重复交易护栏。旧持仓纪律线不会合成 `action_required`。
- A 股生产优化器要求有效 `behavioral_factor`。缺失时先从持仓与关注列表补建横截面；补全失败返回 `WAIT`，快照状态为 `factor_unavailable`，窗口灰卡且不允许交易。
- 反向交易冷却不是固定禁买回：当天或冷却窗口内发生过同标的真实/影子反向成交时，再次买入必须重新触发 `buy_pullback`、`reentry` 或 `buy_breakout`，且当前价要相对上次成交价穿越对应触发边界；再次卖出按委员会/优化器卖出方向和交易约束判断。
- 交易模式在 `jobs/trading_mode.py` 定义，并由 `jobs/market_monitor_alerts.py` 使用：`主动盈利` 保持期望最大化，`现金回收` 增加现金保留和卖出释放现金效用。旧 `risk_off` / “主动避险”会归一化为现金回收；模式只改变提醒优化层，不改变 `entry_exit_points` 或委员会原始 verdict。
- 主窗口文案在 `scripts/monitor_window_text.py`：方向性委员会结果但未成为可执行提醒时显示“候选卖/候选买”，避免和普通“观察”混淆。快照原因来自 `jobs/market_monitor_snapshot.py` 的 `operation.reason`。
- 详情弹窗颜色也在 `scripts/monitor_window_text.py` / `scripts/monitor_window_analysis.py`：风险行红色加粗、正向证据绿色加粗、背景信息灰色。这是 Tk 桌面渲染层，不改变 `utils.phone_committee`、HTTP 或 Android App 的 JSON 接口。
- 主窗口 `technical.ma20/ma120/atr_pct` 正常来自 `jobs/market_monitor_snapshot.py`；只有配置兜底快照才会在 `scripts.monitor_window_services` 里补算，而且只读本地 `market_data.db`。如果维护者发现窗口技术指标为空，应先确认 API 后台预拉或监控任务是否已把历史行情写入本地缓存，不要把历史行情拉取放进 UI 服务。
- 卖出阈值诊断脚本是 `scripts/diagnose_ashare_sell_threshold.py`。它比较历史 `committee_sell_stop_band` 的收益/回撤、卖出胜率下界、卖出后规避回撤和错过反弹成本；结果只用于研究，不代表当前生产提醒逻辑。
- 周度优化的 `policy_quality_score` 会纳入卖出后 5 日路径效用：`avg_post_sell_net_edge_pct = avoided_drawdown - missed_rebound`。该指标只用于历史参数评估。
- 周度参数优化的行业映射会先用更像浏览器的东方财富直连，失败后尝试 AkShare，再读 `data/sector_cache.json`；缓存仍缺失时才使用本地配置里的 `sector`/`industry`。
- 盘中监控会在读取账本/配置后调用 `load_sector_cache_mapping()` 和 `_with_sector_cache()`，对 A 股标的用 `data/sector_cache.json` 覆盖运行时 `sector`，原始配置板块保留在 `config_sector`。因此委员会上下文、告警板块约束和窗口展示使用同一个东方财富板块。
- 行业映射是多源合并，不再因为实时源返回了部分标的就跳过缓存补缺；`sample_quality` 只用于历史止盈止损研究输出，不影响当前生产提醒。
- `AccountLedger(real)` 是持仓单一可信源；ledger 更新后同步 config/snapshot，config 只作为旧路径兼容和新标的首次播种来源。
- `latest_window.json` 只保留 real 的 `analysis_id/decision_id`，并递归剔除 `shadow_result`/`shadow_*`。用户执行推荐手数时，以 real `decision_id + 方向 + 股数` 组成幂等键；重复点击/请求重放返回原成交。

---

## 3. 为什么不合并

**两条路径并存就是 feature，不是债务**。原因：

### 3.1 成本结构不同

| 场景 | Skill 适合 | Web/Cron 适合 |
|------|-----------|---------------|
| 用户主动追问"该不该加仓" | ✅（Claude 自家用，0 成本）| ❌（每次都 ¥0.03）|
| cron 每日 03:00 自动跑 | ❌（用户没在 Claude Code 里）| ✅（DeepSeek 月成本可控）|
| 多资产并行扫一遍 | ❌（用户的 Claude 不擅长长批处理）| ✅（ThreadPool 16s 完成）|

### 3.2 验证机制

**同问题两套 verdict**——验证模型偏差。
比如 Skill 出 BUY、Web 出 HOLD，说明这个判断在不同模型间不收敛，**值得人工复核**。
合并成单路径 → 失去这个对比信号。

### 3.3 容灾

DeepSeek 限速 / 涨价 / 跑路？Coordinator 路径不受影响，用户照常用。
Claude API 抖？Direct 路径不受影响，自动化照跑。

---

## 4. 实现层次（哪些代码两条路径共享）

```
┌─────────────────────────────────────────────────┐
│  agents/{macro,quant,risk,cio}.py 的 prompt     │ ← 共享
├─────────────────────────────────────────────────┤
│  core/regime.py REGIME 硬约束算法                │ ← 共享
├─────────────────────────────────────────────────┤
│  core/committee.py run_committee 编排逻辑        │ ← 仅 Direct 用
│  skill/run.sh prepare_committee 提示生成         │ ← 仅 Coordinator 用
│  skill/run.sh run_committee（包 run_committee） │ ← Direct 在 skill 里的入口
├─────────────────────────────────────────────────┤
│  agents/sdk_agent.py SDKAgent (DeepSeek HTTP)   │ ← 仅 Direct 用
│  Claude Agent({...}) tool                        │ ← 仅 Coordinator 用
└─────────────────────────────────────────────────┘
```

**共享率约 70%**（prompt + REGIME + sanity check + 数据准备）。
**分歧 30%**（编排器 + LLM client）。

加一个新角色（如 ESG 分析师）需要两边都注册——这是一致性的代价。

---

## 5. 升级 Claude Agent SDK？

调研过（2026-05）：可以用 [`anthropics/claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python) 让 Direct 路径也走 Claude 真 subagent。

**问题**：
1. 强绑 Claude 模型（不能 DeepSeek），cron 每日跑成本 30-100×
2. SDK 是 Claude Code CLI 的 subprocess wrapper，需要服务器装 `claude` CLI binary
3. 单次 committee ¥0.5-2，每天 cron = 百块/月

**结论**：暂不升级。详见 [adr/002-no-claude-agent-sdk.md](adr/002-no-claude-agent-sdk.md)。

---

## 6. 决策树：你该用哪条

```
你在 Claude Code 里 + 想问"该不该买"？
  → Coordinator 路径（让你的 Claude spawn 4 subagent，不烧 DeepSeek token）

你在 Cursor / Cline / Codex / 其他非 Claude agent 里？
  → Direct 路径（skill `run_committee SYM` 一键拿 verdict，需要 DEEPSEEK_API_KEY）

你在 Web GUI 想点按钮触发 + 看实时进度？
  → Direct 路径（POST /api/committee/run，自带 SSE）

你想每天 03:00 自动跑（无人值守）？
  → Direct 路径 cron（jobs/daily_report.py）

你想对比同问题两个模型的 verdict 看一致性？
  → 两条都跑一遍
```

---

## 下一步

→ [adr/001-dual-execution-paths.md](adr/001-dual-execution-paths.md) — 为什么这个决定不是 tech debt

→ [adr/002-no-claude-agent-sdk.md](adr/002-no-claude-agent-sdk.md) — 不升级 Claude Agent SDK 的具体原因

→ [02-agents.md](02-agents.md) — 4 个角色 prompt 在两条路径里都长什么样
