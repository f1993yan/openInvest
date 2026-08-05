# jobs/

APScheduler 自动发现的定时任务。每个 `.py` 配套一个 `.yml` 描述 cron 触发器。`scheduler/runner.py` 启动时扫描本目录注册所有 job。

## 内容

| Job | 频率 | 职责 |
|-----|------|------|
| `daily_report.py` | 每天 10am | 跑完整投资委员会，生成 markdown brief，发邮件 |
| `commsec_sync.py` | 每 2h | 拉 CommSec 成交回执邮件，更新 portfolio.holdings |
| `payday_check.py` | 每月 1 日 | 自动 +CNY 入账（配置在 user.md 的薪资字段）|
| `dreaming.py` | 每天 3am | 三阶段记忆整合（Light → REM → Deep Sleep），insights/ 沉淀长期模式 |
| `pnl_snapshot.py` | 工作日每 2h | 算 PnL 写 jsonl 历史，渲染 `docs/pnl_chart.svg` |
| `verdict_review.py` | 每月 1 日 | 月度委员会决策命中率报告 |
| `market_monitor.py` | 交易时段按配置间隔 | 盘中监控入口：拉行情、直接调用委员会、写桌面窗口快照 |

## 盘中监控模块地图

`market_monitor.py` 只是兼容入口，大部分逻辑拆在同目录模块：

| 模块 | 职责 |
|------|------|
| `market_monitor_common.py` | 路径、日志、交易时间常量、通用数值函数 |
| `market_monitor_quotes.py` | 腾讯/新浪行情、A 股行为因子横截面和 5 日目标状态、直接 Python 调用委员会 |
| `market_monitor_notify.py` | Windows 弹框开关和摘要弹出时间规则 |
| `market_monitor_entry_exit.py` | 买卖点连续触发状态；旧持仓止盈止损计划函数仅保留兼容，不被生产监控调用 |
| `market_monitor_guards.py` | 涨停买入保护、重复交易冷却、反向交易价格线护栏 |
| `market_monitor_alerts.py` | 现金/风险约束下的最优操作提醒选择 |
| `market_monitor_snapshot.py` | 主窗口 `latest_window.json` 和本地 markdown 报告 |
| `market_monitor_runtime.py` | 一轮监控编排和主循环 |

关键边界：

- `call_committee()` 直接调用 `backend.server.run_committee_direct()`，不依赖 8766 HTTP。
- `build_behavioral_factor_context()` 对本轮持仓与关注 A 股一次性读取两年历史，生成前四目标、逆波动仓位和每标的独立优化器权重；目标状态写入已忽略的 `data/behavioral_factor_state.json`，每 5 个交易日更新。周日 `behavioral_factor_calculation` 对外写出的 `data/behavioral_factor_assessments.json` 只列前四，完整横截面仅用于内部零目标和委员会判断。
- 每标的权重回看最近 63 个交易日，以五日持有块计算“入选后的横截面超额收益 / 未入选后规避的反向超额收益”，再由 `n/(n+20)` 样本可靠度与后验正收益概率确定。该权重是优化器目标仓位惩罚强度，不是建议买入比例。
- `entry_exit_points` 是入场/出场技术参考，会随行情刷新。
- 成本锚定的 `position_exit_plan` / `position_exit_policy` / `position_exit_discipline_review` 已从生产链路停用：不再影响优化器预期收益、提醒评分、提醒阈值、窗口状态或详情文案。
- 周度持仓纪律任务、YAML 和策略模块已删除；旧 API 字段只保留空对象兼容。
- `market_monitor_alerts.py` 的卖出提醒只接收委员会/优化器 `SELL/TRIM`、负向建议金额、可卖手数、交易模式、T+1 和重复交易护栏；不会由旧持仓纪律线合成卖出候选。
- 真实/影子账户当天或冷却窗口内有同标的反向成交时，系统不一刀切禁止买回；再次买入必须重新触发 `buy_pullback`、`reentry` 或 `buy_breakout`，并且价格相对上次成交价穿越对应边界。再次卖出按委员会/优化器卖出方向和交易约束判断。
- `INVEST_MONITOR_EMAIL_ACTIONS=1` 时，`market_monitor_notify.py` 会在 `action_required` 出现后发送执行提醒邮件，并按交易日、标的、方向、手数和触发来源去重；邮箱凭证仍复用 `EMAIL_SENDER` / `EMAIL_PASSWORD` / `DIGEST_EMAIL_TO`。桌面窗口标题栏的 `邮件开/邮件关` 是运行中动态开关，会写入 `monitor_action_email_enabled=false` 时临时静默执行邮件，但不修改 `.env` 总开关。
- A 股行为因子补全会合并目标标的、持仓和关注列表，并复用 `data/sector_cache.json` 做板块展示；移植时可单独带走该缓存。
- `market_monitor_runtime.py` 运行时也会读取 `data/sector_cache.json`，把 A 股标的的 `sector` 统一成周更用的东方财富板块；原配置板块保留在 `config_sector`，不直接写回 config/ledger。
- `sample_quality=thin/sparse` 只用于历史止盈止损研究输出，不影响当前生产提醒。
- 主窗口消费 `data/market_monitor/latest_window.json`，UI 改动优先看 `scripts/monitor_window_*.py`。
- `.yml` cron 表达式统一使用 `mon-fri` 这类命名星期，避免不同 cron 解析器对 `1-5` 的含义不一致；周度 A 股止盈止损参数优化即使运行也只产出研究数据。
- Windows 控制台输出优先使用 `[INFO]` / `[WARN]` / `[ERROR]` 这类 ASCII 状态前缀，避免 GBK 终端遇到 emoji 日志时报编码错误。

- `INDEX.md` — 所有 job 的输入/输出 spec（人类参考）
- `*.yml` — APScheduler cron 配置（声明式）

## 与其他目录的关系

- 上游：被 `scheduler/runner.py` 注册；也可单跑 `python -m jobs.daily_report`
- 下游：调用 `core/committee.py`、`agents/*`，写 `memory/` 和 `docs/`
- **生产关键路径**：cron 写错会污染真实持仓数据，所有写都走 `with_portfolio_tx`
