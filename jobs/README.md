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
| `weekly_exit_param_optimization.py` | 每周 | 按东方财富板块从关注/持仓样本优化 A 股持仓止盈止损参数 |

## 盘中监控模块地图

`market_monitor.py` 只是兼容入口，大部分逻辑拆在同目录模块：

| 模块 | 职责 |
|------|------|
| `market_monitor_common.py` | 路径、日志、交易时间常量、通用数值函数 |
| `market_monitor_quotes.py` | 腾讯/新浪行情、A 股行为因子横截面和 5 日目标状态、直接 Python 调用委员会 |
| `market_monitor_notify.py` | Windows 弹框开关和摘要弹出时间规则 |
| `market_monitor_entry_exit.py` | 买卖点连续触发状态、A 股持仓止盈止损计划 |
| `market_monitor_guards.py` | 涨停买入保护、重复交易冷却、反向交易价格线护栏 |
| `market_monitor_alerts.py` | 现金/风险约束下的最优操作提醒选择 |
| `market_monitor_snapshot.py` | 主窗口 `latest_window.json` 和本地 markdown 报告 |
| `market_monitor_runtime.py` | 一轮监控编排和主循环 |

关键边界：

- `call_committee()` 直接调用 `backend.server.run_committee_direct()`，不依赖 8766 HTTP。
- `build_behavioral_factor_context()` 对本轮持仓与关注 A 股一次性读取两年历史，生成前四目标、逆波动仓位和每标的独立优化器权重；目标状态写入已忽略的 `data/behavioral_factor_state.json`，每 5 个交易日更新。
- 每标的权重回看最近 63 个交易日，以五日持有块计算“入选后的横截面超额收益 / 未入选后规避的反向超额收益”，再由 `n/(n+20)` 样本可靠度与后验正收益概率确定。该权重是优化器目标仓位惩罚强度，不是建议买入比例。
- `entry_exit_points` 是入场/出场技术参考，会随行情刷新。
- `position_exit_plan` 是持仓后的成本锚定纪律计划，盘中只检查触发，收盘后才允许追踪止损上移。
- `weekly_exit_param_optimization.py` 默认回看最近 62 天；写入的 `policy_quality_score` 使用 Wilson 下界胜率、保守卖出期望、profit factor 和回撤惩罚，校准已有持仓卖出/减仓提醒。
- 同一任务还会写入 `sell_utility_adjustment_pct`、`sell_reliability`、`sell_evidence_score`，供 `core.decision_optimizer` 在已有持仓上评估“继续持有 vs 减仓”的期望效用；证据不足时会收缩到接近 0，不影响空仓买入。
- `market_monitor_alerts.py` 会把已持仓 A 股的纪律触发当成概率证据，而不是机械卖点：止损即时复核，止盈/减仓要求连续确认；最终按“纪律卖出期望 - 委员会继续持有证据 - 交易摩擦”选择 `action_required` 或 `trigger_confirmed`/候选复核。
- 如果同板块监控样本显示共振杀跌，且个股相对板块 MAD Z 值没有显著弱于板块，`market_monitor_alerts.py` 会写入 `discipline_review.sector_panic_guard`，把板块错杀/错过反弹成本计入继续持有证据，避免把板块恐慌当成个股独立破位机械止损。
- 未触发当前持仓纪律线时，强 `SELL/TRIM` 只进入候选/复核，不进入可执行提醒。真实/影子账户当天或冷却窗口内有同标的反向成交时，系统不一刀切禁止买回，但再次买入必须重新触发 `buy_pullback`、`reentry` 或 `buy_breakout`，并且价格相对上次成交价穿越对应边界；再次卖出必须触发当前 `position_exit_plan`。
- `INVEST_MONITOR_EMAIL_ACTIONS=1` 时，`market_monitor_notify.py` 会在 `action_required` 出现后发送执行提醒邮件，并按交易日、标的、方向、手数和触发来源去重；邮箱凭证仍复用 `EMAIL_SENDER` / `EMAIL_PASSWORD` / `DIGEST_EMAIL_TO`。桌面窗口标题栏的 `邮件开/邮件关` 是运行中动态开关，会写入 `monitor_action_email_enabled=false` 时临时静默执行邮件，但不修改 `.env` 总开关。
- `weekly_exit_param_optimization.py` 的行业映射会合并东方财富浏览器请求头直连、AkShare、`data/sector_cache.json` 和本地配置；实时源只返回部分标的时会继续用缓存补缺。移植到新电脑时可把 `data/sector_cache.json` 一起带走。
- `market_monitor_runtime.py` 运行时也会读取 `data/sector_cache.json`，把 A 股标的的 `sector` 统一成周更用的东方财富板块；原配置板块保留在 `config_sector`，不直接写回 config/ledger。
- `sample_quality=thin/sparse` 时只收缩卖出效用字段，不丢弃止盈止损参数，避免单票板块过拟合。
- 主窗口消费 `data/market_monitor/latest_window.json`，UI 改动优先看 `scripts/monitor_window_*.py`。
- `.yml` cron 表达式统一使用 `mon-fri` 这类命名星期，避免不同 cron 解析器对 `1-5` 的含义不一致；周度 A 股止盈止损参数优化安排在周日 20:00。
- Windows 控制台输出优先使用 `[INFO]` / `[WARN]` / `[ERROR]` 这类 ASCII 状态前缀，避免 GBK 终端遇到 emoji 日志时报编码错误。

- `INDEX.md` — 所有 job 的输入/输出 spec（人类参考）
- `*.yml` — APScheduler cron 配置（声明式）

## 与其他目录的关系

- 上游：被 `scheduler/runner.py` 注册；也可单跑 `python -m jobs.daily_report`
- 下游：调用 `core/committee.py`、`agents/*`，写 `memory/` 和 `docs/`
- **生产关键路径**：cron 写错会污染真实持仓数据，所有写都走 `with_portfolio_tx`
